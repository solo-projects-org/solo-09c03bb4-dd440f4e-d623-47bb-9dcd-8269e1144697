"""Pydantic request/response models and shared helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator

#: Demand window length for shift peaks: the top 15-minute average load.
PEAK_WINDOW_SECONDS = 900


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_utc_iso(dt: datetime) -> str:
    """Normalize any aware datetime to a UTC ISO-8601 string."""
    if dt.tzinfo is None:
        raise ValueError("timestamp must include a timezone (use UTC, e.g. 2026-01-01T00:00:00Z)")
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ReadingIn(BaseModel):
    source_id: str = Field(min_length=1, description="来源编号，同一读数来源的唯一标识")
    revision: int = Field(ge=1, description="递增修订号，首次为 1，再次提交必须 +1")
    device_id: str = Field(min_length=1)
    ts: datetime = Field(description="读数时刻（UTC，需带时区）")
    meter_no: str = Field(min_length=1)
    cumulative_kwh: float = Field(ge=0, description="累计电表读数 kWh（非负）")

    @field_validator("source_id", "device_id", "meter_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class HandoverIn(BaseModel):
    source_id: str = Field(min_length=1, description="来源编号")
    revision: int = Field(ge=1, description="递增修订号")
    device_id: str = Field(min_length=1)
    ts: datetime = Field(description="换表时刻（UTC，需带时区）")
    old_meter_no: str = Field(min_length=1)
    old_final_kwh: float = Field(ge=0, description="旧表末值（累计 kWh）")
    new_meter_no: str = Field(min_length=1)
    new_initial_kwh: float = Field(ge=0, description="新表初值（累计 kWh）")

    @field_validator("source_id", "device_id", "old_meter_no", "new_meter_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("new_meter_no")
    @classmethod
    def _distinct(cls, v: str, info):
        old = info.data.get("old_meter_no")
        if old is not None and v == old:
            raise ValueError("new_meter_no must differ from old_meter_no")
        return v


class AcceptOut(BaseModel):
    accepted: bool
    kind: str  # reading | handover
    source_id: str
    revision: int
    idempotent: bool = False
    affected_devices: list[str]
    rebuild: dict


class IntervalOut(BaseModel):
    id: int
    device_id: str
    start_ts: str
    end_ts: str
    start_meter_no: str
    end_meter_no: str
    energy_kwh: float | None
    undeterminable: bool
    reason: str | None
    status: str
    start_source_id: str
    start_revision: int
    end_source_id: str
    end_revision: int
    handover_source_id: str | None
    handover_revision: int | None


class RebuildOut(BaseModel):
    run_id: int
    success: bool
    rebuilt_devices: list[str]
    failed_devices: dict[str, str]
    detail: str | None = None


# ---------------------------------------------------------------------------
# Shift peak (最高 15 分钟需量) models
# ---------------------------------------------------------------------------

class ShiftPeakIn(BaseModel):
    device_id: str = Field(min_length=1, description="设备号")
    shift_start: datetime = Field(description="班次开始（UTC，需带时区）")
    shift_end: datetime = Field(description="班次结束（UTC，需带时区）")

    @field_validator("device_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _check_shift(self) -> "ShiftPeakIn":
        for name in ("shift_start", "shift_end"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} must include a timezone (use UTC)")
        if self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be after shift_start")
        seconds = (self.shift_end - self.shift_start).total_seconds()
        if seconds < PEAK_WINDOW_SECONDS:
            raise ValueError(
                f"shift shorter than {PEAK_WINDOW_SECONDS // 60} minutes: "
                "no 15-minute peak window fits"
            )
        return self


class SourceRef(BaseModel):
    source_id: str
    revision: int


class ShiftPeakSources(BaseModel):
    """Provenance of the published peak: every reading / handover source whose
    interval the peak window touches, plus the interval row ids used."""
    readings: list[SourceRef]
    handovers: list[SourceRef]
    interval_ids: list[int]


class ShiftPeakOut(BaseModel):
    id: int
    device_id: str
    shift_start: str
    shift_end: str
    window_seconds: int
    max_avg_kw: float = Field(description="最高 15 分钟窗口的平均功率 kW")
    peak_window_start: str = Field(description="最早峰窗起点（UTC）")
    peak_window_end: str = Field(description="最早峰窗终点（UTC）")
    sources: ShiftPeakSources
    input_hash: str = Field(description="核算所用现行区间集合的 SHA-256 指纹")
    status: str = Field(description="current=现行有效结论；stale=已被更正取代")
    computed_at: str
    superseded_at: str | None = None
    idempotent: bool = Field(
        default=False,
        description="仅 POST：相同设备+班次且结论仍现行时为幂等重发",
    )
