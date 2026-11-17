"""Domain logic: revision rules, interval settlement, rebuild transactions.

Key guarantees implemented here:

* Submissions are versioned by (source_id, revision).  Revisions must be
  strictly increasing; re-posting the exact same payload is idempotent;
  posting different content under an existing (source_id, revision) is
  rejected with 409.
* As soon as a late reading/correction is *accepted*, every current interval
  of every affected device is marked ``stale`` inside the same transaction.
* The rebuild that follows runs in its own transaction per device.  A failure
  (or a process kill, or a restart) leaves the stale rows in place — old
  results never come back as current, and devices not involved keep serving.
* Undeterminable intervals are stored with ``energy_kwh = NULL``; zero is
  only ever written for a genuinely measured zero difference.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from .errors import ConflictError, RebuildFailure
from .models import HandoverIn, ReadingIn, to_utc_iso, utc_now_iso


def _hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Revisioned submission handling
# ---------------------------------------------------------------------------

def _check_revision(
    conn: sqlite3.Connection,
    table: str,
    source_id: str,
    revision: int,
    content_hash: str,
) -> bool:
    """Validate the revision chain.

    Revisions must be consecutive: the first submission is revision 1 and
    every later submission must use exactly ``latest + 1``.  Re-posting the
    exact same payload under an existing revision is idempotent.  A content
    mismatch on an existing (source_id, revision) or any rollback/skipping is
    rejected with ConflictError (HTTP 409).
    """
    row = conn.execute(
        f"SELECT revision, content_hash FROM {table} WHERE source_id = ? "
        f"ORDER BY revision DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    if row is not None:
        latest = row["revision"]
        if revision < latest:
            raise ConflictError(
                f"revision rollback rejected: latest revision of source "
                f"'{source_id}' is {latest}, got {revision}"
            )
        if revision == latest:
            existing = conn.execute(
                f"SELECT content_hash FROM {table} WHERE source_id = ? AND revision = ?",
                (source_id, revision),
            ).fetchone()
            if existing["content_hash"] != content_hash:
                raise ConflictError(
                    f"content conflict: source '{source_id}' revision {revision} "
                    f"already exists with different content"
                )
            return True  # identical re-submission
        if revision != latest + 1:
            raise ConflictError(
                f"non-consecutive revision rejected: latest revision of source "
                f"'{source_id}' is {latest}, expected {latest + 1}, got {revision}"
            )
    elif revision != 1:
        raise ConflictError(
            f"first revision of source '{source_id}' must be 1, got {revision}"
        )
    return False


def _stale_current_intervals(conn: sqlite3.Connection, device_ids: list[str]) -> int:
    """Immediately revoke current-supply eligibility on affected devices."""
    if not device_ids:
        return 0
    placeholders = ",".join("?" for _ in device_ids)
    cur = conn.execute(
        f"UPDATE intervals SET status = 'stale', "
        f"superseded_at = ? WHERE status = 'current' AND device_id IN ({placeholders})",
        [utc_now_iso(), *device_ids],
    )
    return cur.rowcount


def submit_reading(conn: sqlite3.Connection, payload: ReadingIn) -> dict:
    ts_iso = to_utc_iso(payload.ts)
    content = {
        "device_id": payload.device_id,
        "ts": ts_iso,
        "meter_no": payload.meter_no,
        "cumulative_kwh": payload.cumulative_kwh,
    }
    digest = _hash(content)
    affected: list[str] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        idempotent = _check_revision(conn, "readings", payload.source_id, payload.revision, digest)
        if not idempotent:
            conn.execute(
                "INSERT INTO readings (source_id, revision, device_id, ts, meter_no, "
                "cumulative, content_hash) VALUES (?,?,?,?,?,?,?)",
                (
                    payload.source_id, payload.revision, payload.device_id, ts_iso,
                    payload.meter_no, payload.cumulative_kwh, digest,
                ),
            )
            # The new reading belongs to its own device.  A correction may also
            # move the reading to a different device, so stale whatever device
            # the previous revision pointed at.
            prev = conn.execute(
                "SELECT device_id FROM readings WHERE source_id = ? AND revision < ? "
                "ORDER BY revision DESC LIMIT 1",
                (payload.source_id, payload.revision),
            ).fetchone()
            devices = {payload.device_id}
            if prev is not None:
                devices.add(prev["device_id"])
            affected = sorted(devices)
            _stale_current_intervals(conn, affected)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "kind": "reading",
        "source_id": payload.source_id,
        "revision": payload.revision,
        "idempotent": idempotent,
        "affected_devices": affected,
    }


def submit_handover(conn: sqlite3.Connection, payload: HandoverIn) -> dict:
    ts_iso = to_utc_iso(payload.ts)
    content = {
        "device_id": payload.device_id,
        "ts": ts_iso,
        "old_meter_no": payload.old_meter_no,
        "old_final_kwh": payload.old_final_kwh,
        "new_meter_no": payload.new_meter_no,
        "new_initial_kwh": payload.new_initial_kwh,
    }
    digest = _hash(content)
    affected: list[str] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        idempotent = _check_revision(conn, "handovers", payload.source_id, payload.revision, digest)
        if not idempotent:
            conn.execute(
                "INSERT INTO handovers (source_id, revision, device_id, ts, old_meter_no, "
                "old_final, new_meter_no, new_initial, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    payload.source_id, payload.revision, payload.device_id, ts_iso,
                    payload.old_meter_no, payload.old_final_kwh,
                    payload.new_meter_no, payload.new_initial_kwh, digest,
                ),
            )
            prev = conn.execute(
                "SELECT device_id, old_meter_no, new_meter_no FROM handovers "
                "WHERE source_id = ? AND revision < ? ORDER BY revision DESC LIMIT 1",
                (payload.source_id, payload.revision),
            ).fetchone()
            devices = {payload.device_id}
            if prev is not None:
                devices.add(prev["device_id"])
            affected = sorted(devices)
            _stale_current_intervals(conn, affected)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "kind": "handover",
        "source_id": payload.source_id,
        "revision": payload.revision,
        "idempotent": idempotent,
        "affected_devices": affected,
    }


# ---------------------------------------------------------------------------
# Interval computation
# ---------------------------------------------------------------------------

def _latest_readings(conn: sqlite3.Connection, device_id: str) -> list[sqlite3.Row]:
    """Authoritative readings: highest revision of every source of a device."""
    return conn.execute(
        "WITH latest AS ("
        "  SELECT source_id, MAX(revision) AS max_rev FROM readings "
        "  WHERE device_id = ? GROUP BY source_id"
        ") SELECT r.* FROM readings r JOIN latest l "
        "ON r.source_id = l.source_id AND r.revision = l.max_rev "
        "ORDER BY r.ts ASC, r.id ASC",
        (device_id,),
    ).fetchall()


def _latest_handovers(conn: sqlite3.Connection, device_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "WITH latest AS ("
        "  SELECT source_id, MAX(revision) AS max_rev FROM handovers "
        "  WHERE device_id = ? GROUP BY source_id"
        ") SELECT h.* FROM handovers h JOIN latest l "
        "ON h.source_id = l.source_id AND h.revision = l.max_rev",
        (device_id,),
    ).fetchall()


def _settle_interval(
    conn: sqlite3.Connection,
    device_id: str,
    a: sqlite3.Row,
    b: sqlite3.Row,
) -> dict:
    """Compute the consumption between two adjacent readings.

    Same meter  -> simple difference; a backward reading is undeterminable
                   (never clamped to zero).
    Cross meter -> only settle when exactly one handover of this device falls
                   strictly inside the interval and its meter numbers match
                   both ends; otherwise undeterminable ('handover missing').
    """
    common = {
        "device_id": device_id,
        "start_ts": a["ts"], "end_ts": b["ts"],
        "start_meter_no": a["meter_no"], "end_meter_no": b["meter_no"],
        "start_source_id": a["source_id"], "start_revision": a["revision"],
        "end_source_id": b["source_id"], "end_revision": b["revision"],
        "handover_source_id": None, "handover_revision": None,
    }

    if a["meter_no"] == b["meter_no"]:
        diff = b["cumulative"] - a["cumulative"]
        if diff < 0:
            return {**common, "energy_kwh": None,
                    "reason": "negative_difference: meter reading went backward"}
        return {**common, "energy_kwh": round(diff, 9), "reason": None}

    # Cross-meter interval: look for the unique matching handover inside it.
    matches = conn.execute(
        "SELECT * FROM handovers WHERE device_id = ? AND ts > ? AND ts < ? "
        "AND old_meter_no = ? AND new_meter_no = ? "
        "AND source_id IN (SELECT source_id FROM handovers WHERE device_id = ?) "
        "ORDER BY id",
        (device_id, a["ts"], b["ts"], a["meter_no"], b["meter_no"], device_id),
    ).fetchall()
    # Keep only the latest revision per handover source.
    latest: dict[str, sqlite3.Row] = {}
    for h in matches:
        cur = latest.get(h["source_id"])
        if cur is None or h["revision"] > cur["revision"]:
            latest[h["source_id"]] = h
    unique = list(latest.values())

    if len(unique) == 0:
        return {**common, "energy_kwh": None,
                "reason": "handover_missing: no matching handover inside cross-meter interval"}
    if len(unique) > 1:
        return {**common, "energy_kwh": None,
                "reason": "ambiguous_handover: more than one matching handover inside interval"}

    h = unique[0]
    seg1 = h["old_final"] - a["cumulative"]
    seg2 = b["cumulative"] - h["new_initial"]
    if seg1 < 0 or seg2 < 0:
        return {**common, "energy_kwh": None,
                "handover_source_id": h["source_id"], "handover_revision": h["revision"],
                "reason": "negative_segment: reading backward relative to handover values"}
    return {
        **common,
        "energy_kwh": round(seg1 + seg2, 9),
        "reason": None,
        "handover_source_id": h["source_id"],
        "handover_revision": h["revision"],
    }


def devices_with_stale_intervals(conn: sqlite3.Connection) -> list[str]:
    return [
        r["device_id"]
        for r in conn.execute(
            "SELECT DISTINCT device_id FROM intervals WHERE status = 'stale' ORDER BY 1"
        )
    ]


def rebuild_device(
    conn: sqlite3.Connection,
    device_id: str,
    *,
    fail_device: str | None = None,
    fail_header_devices: set[str] | None = None,
) -> int:
    """Atomically rebuild all intervals of one device.

    Previously stale rows are kept for audit; only ``current`` rows are
    replaced, and new rows are inserted as ``current`` in one transaction.
    An injected failure raises *before* commit to simulate a crash: rollback
    leaves the run at status ``running`` and every previously stale interval
    untouched.  On service restart init_db flips dangling ``running`` runs to
    ``interrupted`` without reviving any interval.
    """
    fail_header_devices = fail_header_devices or set()
    run = conn.execute(
        "INSERT INTO rebuild_runs (device_id, status) VALUES (?, 'running')",
        (device_id,),
    )
    run_id = run.lastrowid
    conn.commit()

    conn.execute("BEGIN IMMEDIATE")
    try:
        if fail_device == device_id or device_id in fail_header_devices:
            raise RebuildFailure(
                device_id,
                "injected failure before commit (test hook); stale intervals retained",
            )

        readings = _latest_readings(conn, device_id)
        # Touch the handover table so its latest revisions are considered even
        # when no cross-meter interval exists yet.
        _latest_handovers(conn, device_id)

        rows = [
            _settle_interval(conn, device_id, readings[i], readings[i + 1])
            for i in range(len(readings) - 1)
        ]

        # Stale rows stay as an audit trail; only current rows are replaced.
        conn.execute(
            "DELETE FROM intervals WHERE device_id = ? AND status = 'current'",
            (device_id,),
        )
        for r in rows:
            conn.execute(
                "INSERT INTO intervals (device_id, start_ts, end_ts, start_meter_no, "
                "end_meter_no, energy_kwh, status, reason, start_source_id, "
                "start_revision, end_source_id, end_revision, handover_source_id, "
                "handover_revision) VALUES (?,?,?,?,?,?,'current',?,?,?,?,?,?,?)",
                (
                    r["device_id"], r["start_ts"], r["end_ts"],
                    r["start_meter_no"], r["end_meter_no"], r["energy_kwh"], r["reason"],
                    r["start_source_id"], r["start_revision"],
                    r["end_source_id"], r["end_revision"],
                    r["handover_source_id"], r["handover_revision"],
                ),
            )
        conn.execute(
            "UPDATE rebuild_runs SET status = 'success', "
            "finished_at = ? WHERE id = ?",
            (utc_now_iso(), run_id),
        )
        conn.commit()
        return len(rows)
    except RebuildFailure:
        # Simulated crash: rollback and deliberately leave the run 'running'
        # so the restart path marks it 'interrupted'.  Stale rows survive.
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE rebuild_runs SET status = 'failed', detail = ?, "
            "finished_at = ? WHERE id = ?",
            (str(exc), utc_now_iso(), run_id),
        )
        conn.commit()
        raise


def rebuild(
    conn: sqlite3.Connection,
    device_id: str | None = None,
    *,
    devices: list[str] | None = None,
    all_devices: bool = False,
    only_stale: bool = False,
    fail_device: str | None = None,
    fail_header_devices: set[str] | None = None,
) -> dict:
    """Rebuild one device, or every device that needs it.

    ``devices`` limits the run to an explicit set (used by automatic rebuilds
    after submissions, including a device's very first reading when no stale
    rows exist yet).  By default all devices that have readings are rebuilt;
    ``only_stale=True`` limits the run to devices currently holding stale
    intervals.  A failure on one device is recorded and does not abort the
    others; stale intervals of a failed device remain queryable as stale and
    never regain current eligibility.
    """
    if device_id is not None:
        targets = [device_id]
    elif devices is not None:
        targets = list(dict.fromkeys(devices))  # de-duplicate, keep order
    elif all_devices or not only_stale:
        targets = [
            r["device_id"]
            for r in conn.execute(
                "SELECT DISTINCT device_id FROM readings ORDER BY 1"
            )
        ]
    else:
        targets = devices_with_stale_intervals(conn)

    rebuilt: list[str] = []
    failed: dict[str, str] = {}
    for dev in targets:
        try:
            n = rebuild_device(
                conn, dev,
                fail_device=fail_device,
                fail_header_devices=fail_header_devices,
            )
            rebuilt.append(f"{dev} ({n} intervals)")
        except RebuildFailure as exc:
            failed[dev] = exc.reason

    success = not failed
    run_row = conn.execute(
        "INSERT INTO rebuild_runs (device_id, status, detail, finished_at) "
        "VALUES (?,?,?,?)",
        (
            device_id,
            "success" if success else "failed",
            json.dumps({"rebuilt": rebuilt, "failed": failed}),
            utc_now_iso(),
        ),
    )
    conn.commit()
    return {
        "run_id": run_row.lastrowid,
        "success": success,
        "rebuilt_devices": rebuilt,
        "failed_devices": failed,
    }


def list_intervals(
    conn: sqlite3.Connection,
    device_id: str | None,
    status: str,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[str] = []
    if status != "all":
        clauses.append("status = ?")
        params.append(status)
    if device_id is not None:
        clauses.append("device_id = ?")
        params.append(device_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT * FROM intervals{where} ORDER BY device_id, start_ts, id",
        params,
    ).fetchall()
