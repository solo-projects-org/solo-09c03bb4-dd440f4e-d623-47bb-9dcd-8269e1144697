"""FastAPI application: meter readings, handovers, intervals, rebuild,
and shift-peak (top 15-minute demand) conclusions.

Swagger UI is served at ``/docs`` (ReDoc at ``/redoc``) and the OpenAPI
document at ``/openapi.json``.
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .database import ConnectionPool
from .errors import ConflictError, RebuildFailure
from .models import (
    AcceptOut,
    HandoverIn,
    IntervalOut,
    ReadingIn,
    RebuildOut,
    ShiftPeakIn,
    ShiftPeakOut,
)
from . import service


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Thread-local SQLite connections; SQLite WAL + busy_timeout guard writes.
        app.state.pool = ConnectionPool(settings.db_path)
        app.state.pool.init_schema()
        # Deliberately NO automatic rebuild on startup: a restart must never
        # resurrect intervals that lost their current-supply eligibility.
        yield
        app.state.pool.close()

    app = FastAPI(
        title="工厂能耗区间结算 API",
        description=(
            "提交按设备区分的累计电表读数与换表交接，按相邻读数生成带来源编号的"
            "区间电量。倒退读数或交接缺失记为无法判定（energy_kwh=null），绝不报零。"
            "班次峰值接口按设备与 UTC 起止核算最高 15 分钟需量：仅用完整覆盖班次"
            "且电量可判定的现行区间，区间内耗电按均匀分布，峰窗可跨读数边界；"
            "读数/交接更正一经受理，旧峰值立即失去当前资格，须重新核算。"
        ),
        version="1.1.0",
        lifespan=lifespan,
    )

    def db(request: Request) -> sqlite3.Connection:
        return request.app.state.pool.get()

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    @app.get("/health", tags=["运维"])
    def health(conn: sqlite3.Connection = Depends(db)):
        row = conn.execute("SELECT COUNT(*) AS n FROM intervals WHERE status='current'").fetchone()
        stale = conn.execute("SELECT COUNT(*) AS n FROM intervals WHERE status='stale'").fetchone()
        return {"status": "ok", "current_intervals": row["n"], "stale_intervals": stale["n"]}

    @app.post("/readings", response_model=AcceptOut, status_code=201, tags=["读数"])
    def post_reading(
        payload: ReadingIn,
        request: Request,
        conn: sqlite3.Connection = Depends(db),
        x_fail_rebuild: bool = Header(default=False, include_in_schema=False),
    ):
        result = service.submit_reading(conn, payload)
        rebuild_info = _auto_rebuild(request, conn, result["affected_devices"], x_fail_rebuild)
        return {**result, "accepted": True, "rebuild": rebuild_info}

    @app.post("/handovers", response_model=AcceptOut, status_code=201, tags=["换表交接"])
    def post_handover(
        payload: HandoverIn,
        request: Request,
        conn: sqlite3.Connection = Depends(db),
        x_fail_rebuild: bool = Header(default=False, include_in_schema=False),
    ):
        result = service.submit_handover(conn, payload)
        rebuild_info = _auto_rebuild(request, conn, result["affected_devices"], x_fail_rebuild)
        return {**result, "accepted": True, "rebuild": rebuild_info}

    def _auto_rebuild(request: Request, conn: sqlite3.Connection, devices: list[str], fail: bool) -> dict:
        if not devices:
            return {"triggered": False}
        fail_set = set(devices) if fail else set()
        out = service.rebuild(
            conn,
            devices=devices,
            fail_device=request.app.state.settings.fail_device,
            fail_header_devices=fail_set,
        )
        out["triggered"] = True
        return out

    @app.post("/rebuild", response_model=RebuildOut, tags=["区间重建"])
    def post_rebuild(
        request: Request,
        device_id: str | None = Query(default=None, description="只重建指定设备"),
        all_devices: bool = Query(default=False, description="强制重建全部设备（默认只重建存在 stale 区间的设备）"),
        x_fail_rebuild: bool = Header(default=False, include_in_schema=False),
        conn: sqlite3.Connection = Depends(db),
    ):
        fail_set: set[str] = set()
        if x_fail_rebuild:
            if device_id:
                fail_set.add(device_id)
            else:
                fail_set.update(service.devices_with_stale_intervals(conn))
        try:
            return service.rebuild(
                conn, device_id=device_id, all_devices=all_devices,
                only_stale=not all_devices and device_id is None,
                fail_device=request.app.state.settings.fail_device,
                fail_header_devices=fail_set,
            )
        except RebuildFailure as exc:  # pragma: no cover - rebuild() records per-device failures
            return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.get("/intervals", response_model=list[IntervalOut], tags=["区间查询"])
    def get_intervals(
        device_id: str | None = Query(default=None),
        status: str = Query(default="current", pattern="^(current|stale|all)$"),
        conn: sqlite3.Connection = Depends(db),
    ):
        rows = service.list_intervals(conn, device_id, status)
        return [
            {
                "id": r["id"],
                "device_id": r["device_id"],
                "start_ts": r["start_ts"],
                "end_ts": r["end_ts"],
                "start_meter_no": r["start_meter_no"],
                "end_meter_no": r["end_meter_no"],
                "energy_kwh": r["energy_kwh"],
                "undeterminable": r["energy_kwh"] is None,
                "reason": r["reason"],
                "status": r["status"],
                "start_source_id": r["start_source_id"],
                "start_revision": r["start_revision"],
                "end_source_id": r["end_source_id"],
                "end_revision": r["end_revision"],
                "handover_source_id": r["handover_source_id"],
                "handover_revision": r["handover_revision"],
            }
            for r in rows
        ]

    @app.get("/rebuild-runs", tags=["运维"])
    def get_rebuild_runs(conn: sqlite3.Connection = Depends(db)):
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM rebuild_runs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        ]

    @app.post(
        "/shift-peaks",
        response_model=ShiftPeakOut,
        status_code=201,
        tags=["班次峰值"],
        summary="提交班次峰值核算（最高 15 分钟需量）",
    )
    def post_shift_peak(
        payload: ShiftPeakIn,
        conn: sqlite3.Connection = Depends(db),
    ):
        """按设备号与 UTC 起止核算并发布班次峰值结论。

        仅用完整覆盖班次且电量可判定的现行区间，区间内耗电按均匀分布，
        峰窗为任意连续 15 分钟（可跨读数边界）；返回最大平均 kW、最早峰窗
        及涉及的读数/交接来源。班次不足 15 分钟 → 422；缺段、电量无法判定
        或设备待重建 → 409，不发布结论。相同设备+班次在结论仍现行时幂等。
        """
        return service.compute_shift_peak(conn, payload)

    @app.get(
        "/shift-peaks",
        response_model=list[ShiftPeakOut],
        tags=["班次峰值"],
        summary="查询已发布的班次峰值结论",
    )
    def get_shift_peaks(
        device_id: str | None = Query(default=None),
        status: str = Query(default="current", pattern="^(current|stale|all)$"),
        conn: sqlite3.Connection = Depends(db),
    ):
        return service.list_shift_peaks(conn, device_id, status)

    app.state.settings = settings
    return app


app = create_app()
