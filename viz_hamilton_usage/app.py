import os
import datetime as dt
from typing import Dict, List, Optional

from flask import Flask, jsonify, send_from_directory
import mysql.connector as mc

try:
    from .config import db_config, TEST_MACHINES, H_ALIAS_NAMES
except Exception:
    # Support running as plain script: python viz_hamilton_usage/app.py
    try:
        from config import db_config, TEST_MACHINES, H_ALIAS_NAMES
    except Exception:
        from config import db_config, TEST_MACHINES  # type: ignore
        H_ALIAS_NAMES = {}


app = Flask(__name__, static_folder="static", static_url_path="/static")


def get_conn(database: str):
    cfg = db_config().copy()
    cfg["database"] = database
    return mc.connect(**cfg)


def _fetchall_dict(cur) -> List[Dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def list_hamilton_ids() -> List[str]:
    # Authoritative list of Hamilton instrument IDs (H3, H7, H14, etc.)
    cn = get_conn("lab_scheduler")
    try:
        cur = cn.cursor()
        cur.execute("SELECT instrument_id FROM instruments WHERE type='hamilton'")
        ids = [r[0] for r in cur.fetchall()]
        return sorted(ids, key=lambda x: (len(x), x))
    finally:
        cn.close()


def find_latest_occupation_for(instrument_id: str) -> Optional[Dict]:
    # Match a range of naming styles seen in operation_data.instrument_ocupation
    # e.g., 'H13', 'E606_H8', 'D184_H7', 'H12_293H', with optional _Simulation suffix
    suffix = instrument_id if instrument_id.startswith("H") else instrument_id
    candidates = [
        instrument_id,
        f"{instrument_id}_Simulation",
        f"%_H{instrument_id[1:]}%" if instrument_id.startswith("H") else None,
        f"%{instrument_id}%",
    ]
    candidates = [c for c in candidates if c]

    cn = get_conn("operation_data")
    try:
        cur = cn.cursor()
        # Build a UNION of LIKE/equals to get the freshest match for this Hamilton
        # We limit to the latest 1 row overall across patterns.
        where_clauses = []
        params: List[str] = []
        for c in candidates:
            if "%" in c:
                where_clauses.append("Instrument LIKE %s")
            else:
                where_clauses.append("Instrument = %s")
            params.append(c)
        where = " OR ".join(f"({w})" for w in where_clauses)
        sql = (
            "SELECT Instrument, test_name, operator, start_time, finish_time, status, "
            "well_plate_id, plate_index, plate_count "
            "FROM instrument_ocupation WHERE "
            + where +
            " ORDER BY start_time DESC LIMIT 1"
        )
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        cn.close()


def infer_state(occ: Optional[Dict]) -> str:
    if not occ:
        return "unknown"
    status = (occ.get("status") or "").strip().lower()
    finish_time = occ.get("finish_time")
    # Busy if unfinished or marked as start/running/busy
    if finish_time is None:
        return "busy"
    try:
        now = dt.datetime.now()
        if isinstance(finish_time, str):
            finish_dt = dt.datetime.fromisoformat(finish_time)
        else:
            finish_dt = finish_time
        if finish_dt and finish_dt > now:
            return "busy"
    except Exception:
        pass
    if status in {"start", "running", "busy"}:
        return "busy"
    return "idle"


def age_human(ts: Optional[dt.datetime]) -> Optional[str]:
    if not ts:
        return None
    delta = dt.datetime.now() - ts
    # Clamp negatives (timezone/clock skews) to zero
    if delta.total_seconds() < 0:
        return "0m"
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    return f"{hours}h {mins % 60}m"


# instrument_status is deprecated; all logic uses instrument_ocupation only.


@app.route("/api/status")
def api_status():
    # Build canonical H-series entries from lab_scheduler using instrument_ocupation
    ids = list_hamilton_ids()
    items_by_id: Dict[str, Dict] = {}
    for hid in ids:
        occ = find_latest_occupation_for(hid)
        state = infer_state(occ)
        item = {
            "id": hid,
            "state": state,
            "details": {
                "test_name": occ.get("test_name") if occ else None,
                "operator": occ.get("operator") if occ else None,
                "plate_id": occ.get("well_plate_id") if occ else None,
                "plate_index": occ.get("plate_index") if occ else None,
                "plate_count": occ.get("plate_count") if occ else None,
                "start_time": occ.get("start_time").isoformat() if occ and occ.get("start_time") else None,
                "finish_time": occ.get("finish_time").isoformat() if occ and occ.get("finish_time") else None,
                "raw_instrument_name": occ.get("Instrument") if occ else None,
            },
            "is_test": hid in TEST_MACHINES,
            "last_event_age": age_human((occ or {}).get("finish_time") or (occ or {}).get("start_time")),
            "source": "instrument_ocupation" if occ else "none",
        }
        # attach static aliases (e.g., ELISA names) for display only
        aliases = H_ALIAS_NAMES.get(hid)
        if aliases:
            item["details"]["aliases"] = list(aliases)
        items_by_id[hid] = item

    # Collate final items: H-series only
    out: List[Dict] = list(items_by_id.values())

    # Sort busy first, then by id
    def sort_key(x):
        return (0 if x["state"] == "busy" else 1, x["id"])

    out_sorted = sorted(out, key=sort_key)
    return jsonify({
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "items": out_sorted,
    })


@app.route("/")
def index_page():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
