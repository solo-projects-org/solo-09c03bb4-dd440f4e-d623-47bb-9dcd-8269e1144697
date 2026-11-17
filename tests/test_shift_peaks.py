"""End-to-end tests for the shift-peak (top 15-minute demand) API."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def ctx():
    tmp = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmp.name, "test.db")
    settings = Settings(db_path=db_path, fail_device=None)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, db_path, tmp
    tmp.cleanup()


def reading(source, rev, device, ts, meter, kwh):
    return {
        "source_id": source, "revision": rev, "device_id": device,
        "ts": ts, "meter_no": meter, "cumulative_kwh": kwh,
    }


def handover(source, rev, device, ts, old_m, old_v, new_m, new_v):
    return {
        "source_id": source, "revision": rev, "device_id": device, "ts": ts,
        "old_meter_no": old_m, "old_final_kwh": old_v,
        "new_meter_no": new_m, "new_initial_kwh": new_v,
    }


def post_peak(client, device, start, end):
    return client.post(
        "/shift-peaks",
        json={"device_id": device, "shift_start": start, "shift_end": end},
    )


def peaks(client, device=None, status="current"):
    params = {"status": status}
    if device:
        params["device_id"] = device
    return client.get("/shift-peaks", params=params).json()


def setup_hourly(client, device="D1", prefix="S"):
    """30 kW / 60 kW / 10 kW hourly profile on one meter."""
    client.post("/readings", json=reading(f"{prefix}1", 1, device, "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading(f"{prefix}2", 1, device, "2026-01-01T01:00:00Z", "M1", 130.0))
    client.post("/readings", json=reading(f"{prefix}3", 1, device, "2026-01-01T02:00:00Z", "M1", 190.0))
    client.post("/readings", json=reading(f"{prefix}4", 1, device, "2026-01-01T03:00:00Z", "M1", 200.0))


# ---------------------------------------------------------------------------
# 1. Happy path: max average kW, earliest window, provenance
# ---------------------------------------------------------------------------

def test_peak_basic_earliest_window_and_provenance(ctx):
    client, _, _ = ctx
    setup_hourly(client)
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T03:00:00Z")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "current" and body["idempotent"] is False
    assert body["window_seconds"] == 900
    # Highest 15-min average sits inside the 60 kW hour, earliest at 01:00.
    assert body["max_avg_kw"] == pytest.approx(60.0)
    assert body["peak_window_start"] == "2026-01-01T01:00:00.000Z"
    assert body["peak_window_end"] == "2026-01-01T01:15:00.000Z"
    # The peak window touches only the second interval -> readings S2/S3.
    assert body["sources"]["readings"] == [
        {"source_id": "S2", "revision": 1},
        {"source_id": "S3", "revision": 1},
    ]
    assert body["sources"]["handovers"] == []
    assert len(body["sources"]["interval_ids"]) == 1
    assert body["input_hash"] and body["computed_at"]

    # Querying published conclusions returns the same row.
    got = peaks(client, "D1")
    assert len(got) == 1 and got[0]["id"] == body["id"]
    assert got[0]["max_avg_kw"] == pytest.approx(60.0)


def test_peak_window_crosses_reading_boundaries(ctx):
    client, _, _ = ctx
    # 10-minute readings: 30 kW, 120 kW, 30 kW.
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 0.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T00:10:00Z", "M1", 5.0))
    client.post("/readings", json=reading("S3", 1, "D1", "2026-01-01T00:20:00Z", "M1", 25.0))
    client.post("/readings", json=reading("S4", 1, "D1", "2026-01-01T00:30:00Z", "M1", 30.0))

    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T00:30:00Z")
    assert r.status_code == 201, r.text
    body = r.json()
    # Best 15-min windows straddle the 10-min boundaries: 00:05-00:20 and
    # 00:10-00:25 both average 90 kW; the earliest one wins.
    assert body["max_avg_kw"] == pytest.approx(90.0)
    assert body["peak_window_start"] == "2026-01-01T00:05:00.000Z"
    assert body["peak_window_end"] == "2026-01-01T00:20:00.000Z"
    # The window spans two intervals -> three reading sources involved.
    assert body["sources"]["readings"] == [
        {"source_id": "S1", "revision": 1},
        {"source_id": "S2", "revision": 1},
        {"source_id": "S3", "revision": 1},
    ]
    assert len(body["sources"]["interval_ids"]) == 2


def test_peak_tie_on_flat_profile_starts_at_shift_start(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T01:00:00Z", "M1", 130.0))
    client.post("/readings", json=reading("S3", 1, "D1", "2026-01-01T02:00:00Z", "M1", 160.0))
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
    assert r.status_code == 201
    body = r.json()
    assert body["max_avg_kw"] == pytest.approx(30.0)
    assert body["peak_window_start"] == "2026-01-01T00:00:00.000Z"


def test_shift_exactly_fifteen_minutes_is_allowed(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 0.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T00:15:00Z", "M1", 7.5))
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z")
    assert r.status_code == 201
    body = r.json()
    assert body["max_avg_kw"] == pytest.approx(30.0)
    assert body["peak_window_start"] == "2026-01-01T00:00:00.000Z"
    assert body["peak_window_end"] == "2026-01-01T00:15:00.000Z"


def test_shift_shorter_than_window_is_rejected(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 0.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T00:15:00Z", "M1", 7.5))
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T00:14:59Z")
    assert r.status_code == 422
    # Inverted or naive timestamps are rejected at validation too.
    assert post_peak(client, "D1", "2026-01-01T00:15:00Z", "2026-01-01T00:00:00Z").status_code == 422
    assert post_peak(client, "D1", "2026-01-01T00:00:00", "2026-01-01T01:00:00").status_code == 422
    assert peaks(client, "D1") == []


# ---------------------------------------------------------------------------
# 2. No-publish conditions: coverage gaps, undeterminable energy, rebuild
# ---------------------------------------------------------------------------

def test_insufficient_coverage_is_rejected(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T01:00:00Z", "M1", 130.0))

    # Shift runs past the last reading.
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
    assert r.status_code == 409 and "insufficient coverage" in r.json()["detail"]
    # Shift starts before the first reading.
    r = post_peak(client, "D1", "2025-12-31T23:30:00Z", "2026-01-01T01:00:00Z")
    assert r.status_code == 409 and "insufficient coverage" in r.json()["detail"]
    # Unknown device has no current intervals at all.
    r = post_peak(client, "D9", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    assert r.status_code == 409 and "insufficient coverage" in r.json()["detail"]
    assert peaks(client, "D1") == []


def test_undeterminable_interval_blocks_only_overlapping_shifts(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    # Backward reading -> first interval undeterminable (never zero).
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T01:00:00Z", "M1", 90.0))
    client.post("/readings", json=reading("S3", 1, "D1", "2026-01-01T02:00:00Z", "M1", 120.0))

    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
    assert r.status_code == 409 and "undeterminable energy" in r.json()["detail"]

    # A shift overlapping only the determinable interval publishes fine.
    r = post_peak(client, "D1", "2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z")
    assert r.status_code == 201
    assert r.json()["max_avg_kw"] == pytest.approx(30.0)


def test_cross_meter_peak_uses_handover_and_requires_it(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M-old", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M-new", 40.0))

    # Cross-meter interval without handover -> undeterminable -> no publish.
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
    assert r.status_code == 409 and "undeterminable energy" in r.json()["detail"]

    client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M-new", 10.0))
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
    assert r.status_code == 201, r.text
    body = r.json()
    # (150-100)+(40-10) = 80 kWh over 2 h -> uniform 40 kW.
    assert body["max_avg_kw"] == pytest.approx(40.0)
    assert body["sources"]["handovers"] == [{"source_id": "H1", "revision": 1}]


def test_pending_rebuild_blocks_publish(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    assert post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z").status_code == 201

    # Late reading with an injected rebuild failure: the acceptance stales the
    # old peak immediately and the device stays pending rebuild.
    r = client.post(
        "/readings", headers={"X-Fail-Rebuild": "true"},
        json=reading("S9", 1, "D1", "2026-01-01T01:00:00Z", "M1", 112.0),
    )
    assert r.json()["rebuild"]["success"] is False

    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
    assert r.status_code == 409 and "rebuild pending" in r.json()["detail"]
    assert peaks(client, "D1") == []
    stale = peaks(client, "D1", status="stale")
    assert len(stale) == 1 and stale[0]["superseded_at"]


# ---------------------------------------------------------------------------
# 3. Corrections invalidate published peaks; recalculation is explicit
# ---------------------------------------------------------------------------

def test_reading_correction_stales_peak_and_recalculation_replaces(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    first = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z").json()
    assert first["max_avg_kw"] == pytest.approx(15.0)

    # Correction accepted -> the published peak loses current eligibility at once.
    client.post("/readings", json=reading("S2", 2, "D1", "2026-01-01T02:00:00Z", "M1", 160.0))
    assert peaks(client, "D1") == []
    stale = peaks(client, "D1", status="stale")
    assert [p["id"] for p in stale] == [first["id"]]

    # Explicit recalculation publishes a new conclusion from the new intervals.
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
    assert r.status_code == 201 and r.json()["idempotent"] is False
    second = r.json()
    assert second["id"] != first["id"]
    assert second["max_avg_kw"] == pytest.approx(30.0)
    assert second["input_hash"] != first["input_hash"]
    assert second["sources"]["readings"] == [
        {"source_id": "S1", "revision": 1},
        {"source_id": "S2", "revision": 2},
    ]
    both = peaks(client, "D1", status="all")
    assert sorted(p["status"] for p in both) == ["current", "stale"]


def test_handover_correction_stales_peak(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M-old", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M-new", 40.0))
    client.post("/handovers", json=handover(
        "H1", 1, "D1", "2026-01-01T01:00:00Z", "M-old", 150.0, "M-new", 10.0))
    assert post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z").status_code == 201

    client.post("/handovers", json=handover(
        "H1", 2, "D1", "2026-01-01T01:00:00Z", "M-old", 160.0, "M-new", 10.0))
    assert peaks(client, "D1") == []
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
    # (160-100)+(40-10) = 90 kWh over 2 h -> 45 kW, handover revision 2.
    assert r.json()["max_avg_kw"] == pytest.approx(45.0)
    assert r.json()["sources"]["handovers"] == [{"source_id": "H1", "revision": 2}]


def test_restart_and_retry_do_not_restore_old_conclusion(ctx):
    client, db_path, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    assert post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z").status_code == 201

    client.post(
        "/readings", headers={"X-Fail-Rebuild": "true"},
        json=reading("S9", 1, "D1", "2026-01-01T01:00:00Z", "M1", 112.0),
    )

    # Full restart against the same DB file: nothing is resurrected.
    app2 = create_app(Settings(db_path=db_path, fail_device=None))
    with TestClient(app2) as client2:
        assert peaks(client2, "D1") == []
        assert post_peak(client2, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z").status_code == 409

        # Retry the rebuild: intervals come back, the old peak does not.
        assert client2.post("/rebuild", params={"device_id": "D1"}).json()["success"] is True
        assert peaks(client2, "D1") == []
        assert len(peaks(client2, "D1", status="stale")) == 1

        # A fresh calculation is required and now succeeds: the late reading
        # split the shift into 12 kWh + 18 kWh hours, so the peak is 18 kW.
        r = post_peak(client2, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
        assert r.status_code == 201
        assert r.json()["max_avg_kw"] == pytest.approx(18.0)
        assert len(peaks(client2, "D1")) == 1


def test_unrelated_device_keeps_serving_peaks(ctx):
    client, _, _ = ctx
    client.post("/readings", json=reading("S1", 1, "D1", "2026-01-01T00:00:00Z", "M1", 100.0))
    client.post("/readings", json=reading("S2", 1, "D1", "2026-01-01T02:00:00Z", "M1", 130.0))
    client.post("/readings", json=reading("K1", 1, "D2", "2026-01-01T00:00:00Z", "N1", 200.0))
    client.post("/readings", json=reading("K2", 1, "D2", "2026-01-01T02:00:00Z", "N1", 260.0))
    assert post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z").status_code == 201
    assert post_peak(client, "D2", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z").status_code == 201

    # A failing rebuild on D1 must not touch D2's published conclusion.
    client.post(
        "/readings", headers={"X-Fail-Rebuild": "true"},
        json=reading("S9", 1, "D1", "2026-01-01T01:00:00Z", "M1", 112.0),
    )
    assert peaks(client, "D1") == []
    d2 = peaks(client, "D2")
    assert len(d2) == 1 and d2[0]["max_avg_kw"] == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# 4. Idempotency and query filters
# ---------------------------------------------------------------------------

def test_identical_resubmission_is_idempotent(ctx):
    client, _, _ = ctx
    setup_hourly(client)
    first = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T03:00:00Z").json()
    r = post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T03:00:00Z")
    assert r.status_code == 201
    second = r.json()
    assert second["idempotent"] is True
    assert second["id"] == first["id"]
    assert second["input_hash"] == first["input_hash"]
    # Equivalent UTC offset form normalizes to the same shift.
    r = post_peak(client, "D1", "2026-01-01T08:00:00+08:00", "2026-01-01T11:00:00+08:00")
    assert r.json()["idempotent"] is True and r.json()["id"] == first["id"]
    assert len(peaks(client, "D1", status="all")) == 1


def test_shift_peak_query_filters(ctx):
    client, _, _ = ctx
    setup_hourly(client, "D1")
    setup_hourly(client, "D2", prefix="K")
    post_peak(client, "D1", "2026-01-01T00:00:00Z", "2026-01-01T03:00:00Z")
    post_peak(client, "D2", "2026-01-01T00:00:00Z", "2026-01-01T03:00:00Z")
    # Stale D1's peak via a correction.
    client.post("/readings", json=reading("S3", 2, "D1", "2026-01-01T02:00:00Z", "M1", 195.0))

    assert len(peaks(client)) == 1                      # default: current only
    assert peaks(client)[0]["device_id"] == "D2"
    assert len(peaks(client, status="stale")) == 1
    assert len(peaks(client, status="all")) == 2
    assert len(peaks(client, "D1", status="stale")) == 1
    assert peaks(client, "D1") == []


def test_openapi_lists_shift_peak_endpoints(ctx):
    client, _, _ = ctx
    spec = client.get("/openapi.json").json()
    assert "/shift-peaks" in spec["paths"]
    assert client.get("/docs").status_code == 200
