#!/usr/bin/env python3
"""
qc-enrich-summaries.py

Read consolidated run summaries CSV and enrich with read-only data from MySQL.

Conventions:
- Uses mysql-connector-python; enforces read-only session.
- DSN file is simple INI-like (key=value) with SERVER, PORT, USER, PASSWORD, DATABASE (optional).
- If the database or expected tables are not present, the script will proceed and write an output CSV with enrichment columns empty.

Adds columns:
- instrument_status_start: Last known instrument status at or before run start (per machine)
- occupied_during_run: 1 if any occupation interval overlaps run window (if available), else 0
- assay: Detected assay (from tracking table if available)
- plate_id: Plate identifier linked to the run (if available)
- enrich_source_db: Database used for enrichment (or empty)

Safety:
- Uses START TRANSACTION READ ONLY and never performs writes.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from typing import Dict, List, Optional, Tuple


def parse_args() -> argparse.Namespace:
    # Keep the CLI explicit because this script is used both directly and
    # through the PowerShell wrapper in scheduled pipeline runs.
    p = argparse.ArgumentParser(description="Enrich run summaries with MySQL data (read-only)")
    p.add_argument("--input-csv", default=os.path.join("outbox", "run-summaries.csv"), help="Input consolidated CSV")
    p.add_argument("--out-csv", default=os.path.join("outbox", "run-summaries-enriched.csv"), help="Output CSV path")
    p.add_argument("--dsn-file", default=os.path.join("config", "mysql_labsite.dsn"), help="Path to DSN file with MySQL creds")
    p.add_argument("--database", default=None, help="Primary MySQL database/schema for status/occupation")
    p.add_argument("--tests-database", default=None, help="Optional schema for test/assay names (e.g., vibrant_test_tracking)")
    p.add_argument("--limit", type=int, default=None, help="Optional limit of runs to process for testing")
    p.add_argument("--status-table", default=None, help="Override instrument status table name (schema.table or table)")
    p.add_argument("--occupation-table", default=None, help="Override instrument occupation table name")
    p.add_argument("--runtime-table", default=None, help="Override instrument runtime table name (for plate_id)")
    p.add_argument("--tracking-table", default=None, help="Override test tracking table name")
    p.add_argument("--aliases-file", default=os.path.join("config", "machine-aliases.json"), help="JSON mapping of H-machines to DB instrument names/codes")
    p.add_argument("--skip-db", action="store_true", help="Skip all DB lookups and still write an output CSV with blank enrichment columns")
    return p.parse_args()


def parse_dsn(path: str) -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    if not os.path.exists(path):
        return cfg
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip().upper()] = v.strip()
    return cfg


def parse_iso8601(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        # Handle 'Z' suffix
        if s.endswith("Z"):
            return dt.datetime.fromisoformat(s[:-1] + "+00:00")
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def load_rows(path: str, limit: Optional[int] = None) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    # PowerShell Export-Csv commonly emits a UTF-8 BOM. utf-8-sig strips it so
    # the first header remains run_id instead of a BOM-prefixed variant.
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
    return rows


def save_rows(path: str, rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    # The output file is the contract for downstream reporting. Always create
    # parent directories so skip-db mode can still materialize a usable CSV.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def load_aliases(path: str) -> Dict[str, List[str]]:
    import json
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Normalize values to list[str]
            norm = {}
            for k, v in data.items():
                if isinstance(v, list):
                    norm[k] = [str(x) for x in v]
                elif isinstance(v, str):
                    norm[k] = [v]
            return norm
    except Exception:
        return {}


def connect_mysql_readonly(dsn: Dict[str, str], database: Optional[str]):
    try:
        import mysql.connector  # type: ignore
    except Exception as e:  # pragma: no cover
        print("ERROR: mysql-connector-python not installed. Install with: python -m pip install mysql-connector-python", file=sys.stderr)
        raise

    params = {
        "host": dsn.get("SERVER") or dsn.get("HOST") or "localhost",
        "port": int(dsn.get("PORT", "3306")),
        "user": dsn.get("USER") or dsn.get("UID") or "",
        "password": dsn.get("PASSWORD") or dsn.get("PWD") or "",
        "database": database or dsn.get("DATABASE") or dsn.get("DB") or None,
        "connection_timeout": 5,
        "autocommit": False,
    }

    cnx = mysql.connector.connect(**params)
    # Enforce read-only at the session level. If the server ignores the command,
    # we still protect ourselves by issuing SELECT queries only.
    try:
        cur = cnx.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cnx.start_transaction(readonly=True)
        cur.close()
    except Exception:
        # If server does not support the statement, proceed; we still only issue SELECTs.
        pass
    return cnx


def resolve_table(cnx, db: str, override: Optional[str], candidates: List[str]) -> Optional[Tuple[str, str]]:
    """Return (schema, table) if a match is found."""
    if override:
        if "." in override:
            sch, tbl = override.split(".", 1)
        else:
            sch, tbl = db, override
        return (sch, tbl)

    q = (
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN (" + ",".join(["%s"] * len(candidates)) + ")"
    )
    args = [db] + candidates
    cur = cnx.cursor()
    cur.execute(q, args)
    row = cur.fetchone()
    cur.close()
    if row:
        return (row[0], row[1])
    return None


def get_columns(cnx, schema: str, table: str) -> List[str]:
    cur = cnx.cursor()
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
        (schema, table),
    )
    cols = [r[0] for r in cur.fetchall()]
    cur.close()
    return cols


def pick_first(columns: List[str], candidates: List[str]) -> Optional[str]:
    cols_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    return None


def fetch_status_maps(cnx, db: str, status_tbl: Tuple[str, str], machines: List[str], start_min: dt.datetime, end_max: dt.datetime) -> Dict[str, List[Tuple[dt.datetime, str]]]:
    schema, table = status_tbl
    cols = get_columns(cnx, schema, table)
    machine_col = pick_first(cols, ["machine", "machine_name", "instrument", "instrument_name", "host", "hostname"])
    ts_col = pick_first(cols, ["ts", "timestamp", "time", "created_at", "updated_at", "event_time", "dt", "utc_time"])
    status_col = pick_first(cols, ["status", "state", "instrument_status", "value"]) or cols[0]

    if not machine_col or not ts_col:
        return {}

    cur = cnx.cursor()
    placeholders = ",".join(["%s"] * len(machines))
    q = (
        f"SELECT {machine_col}, {ts_col}, {status_col} FROM `{schema}`.`{table}` "
        f"WHERE {machine_col} IN ({placeholders}) AND {ts_col} BETWEEN %s AND %s "
        f"ORDER BY {machine_col}, {ts_col}"
    )
    args = machines + [start_min, end_max]
    cur.execute(q, args)
    status_map: Dict[str, List[Tuple[dt.datetime, str]]] = {}
    for m, t, s in cur.fetchall():
        # Normalize to aware UTC if naive
        if isinstance(t, str):
            tdt = parse_iso8601(t) or dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        elif isinstance(t, dt.datetime):
            tdt = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        else:
            tdt = dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        status_map.setdefault(str(m), []).append((tdt, str(s)))
    cur.close()
    return status_map


def fetch_occupation_intervals(cnx, db: str, occ_tbl: Tuple[str, str], machines: List[str], start_min: dt.datetime, end_max: dt.datetime) -> Dict[str, List[Tuple[dt.datetime, dt.datetime]]]:
    schema, table = occ_tbl
    cols = get_columns(cnx, schema, table)
    machine_col = pick_first(cols, ["machine", "machine_name", "instrument", "instrument_name", "host", "hostname"])
    start_col = pick_first(cols, ["start_ts", "start_time", "start", "begin_ts", "begin_time", "created_at"])
    end_col = pick_first(cols, ["end_ts", "end_time", "end", "stop_ts", "stop_time", "updated_at"])
    if not machine_col or not start_col or not end_col:
        return {}

    cur = cnx.cursor()
    placeholders = ",".join(["%s"] * len(machines))
    q = (
        f"SELECT {machine_col}, {start_col}, {end_col} FROM `{schema}`.`{table}` "
        f"WHERE {machine_col} IN ({placeholders}) AND ({start_col} <= %s AND {end_col} >= %s)"
    )
    args = machines + [end_max, start_min]
    cur.execute(q, args)
    occ_map: Dict[str, List[Tuple[dt.datetime, dt.datetime]]] = {}
    for m, s, e in cur.fetchall():
        if isinstance(s, str):
            sdt = parse_iso8601(s) or dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        elif isinstance(s, dt.datetime):
            sdt = s if s.tzinfo else s.replace(tzinfo=dt.timezone.utc)
        else:
            sdt = dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        if isinstance(e, str):
            edt = parse_iso8601(e) or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        elif isinstance(e, dt.datetime):
            edt = e if e.tzinfo else e.replace(tzinfo=dt.timezone.utc)
        else:
            edt = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        occ_map.setdefault(str(m), []).append((sdt, edt))
    cur.close()
    return occ_map


def fetch_status_intervals(cnx, db: str, status_tbl: Tuple[str, str], machines: List[str], start_min: dt.datetime, end_max: dt.datetime) -> Dict[str, List[Tuple[dt.datetime, dt.datetime, str]]]:
    """Fetch status intervals (start,end,status) per instrument when a table stores ranges, e.g., operation_data.instrument_status."""
    schema, table = status_tbl
    cols = get_columns(cnx, schema, table)
    machine_col = pick_first(cols, ["machine", "machine_name", "instrument", "instrument_name", "host", "hostname", "Instrument_name"]) or None
    start_col = pick_first(cols, ["start_ts", "start_time", "start", "begin_ts", "begin_time", "created_at"]) or None
    end_col = pick_first(cols, ["end_ts", "end_time", "end", "stop_ts", "stop_time", "updated_at"]) or None
    status_col = pick_first(cols, ["status", "state", "instrument_status", "value"]) or None
    if not machine_col or not start_col or not end_col or not status_col:
        return {}
    cur = cnx.cursor()
    placeholders = ",".join(["%s"] * len(machines)) if machines else "%s"
    q = (
        f"SELECT {machine_col}, {start_col}, {end_col}, {status_col} FROM `{schema}`.`{table}` "
        f"WHERE {machine_col} IN ({placeholders}) AND ({start_col} <= %s AND {end_col} >= %s)"
    )
    args = machines + [end_max, start_min]
    cur.execute(q, args)
    out: Dict[str, List[Tuple[dt.datetime, dt.datetime, str]]] = {}
    for m, s, e, st in cur.fetchall():
        def to_dt(x):
            if isinstance(x, str):
                return parse_iso8601(x) or dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
            if isinstance(x, dt.datetime):
                return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
            return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        out.setdefault(str(m), []).append((to_dt(s), to_dt(e), str(st)))
    cur.close()
    return out


def fetch_runtime_intervals(cnx, db: str, rt_tbl: Tuple[str, str], instruments: List[str], start_min: dt.datetime, end_max: dt.datetime):
    schema, table = rt_tbl
    cols = get_columns(cnx, schema, table)
    inst_col = pick_first(cols, ["instrument", "instrument_name", "machine", "host", "hostname"]) or None
    start_col = pick_first(cols, ["start_ts", "start_time", "start", "begin_ts", "begin_time", "created_at"]) or None
    end_col = pick_first(cols, ["end_ts", "end_time", "end", "stop_ts", "stop_time", "updated_at"]) or None
    plate_col = pick_first(cols, ["plate_id", "plate", "well_plate_id"]) or None
    if not inst_col or not start_col or not end_col:
        return {}
    cur = cnx.cursor()
    placeholders = ",".join(["%s"] * len(instruments)) if instruments else "%s"
    q = (
        f"SELECT {inst_col}, {start_col}, {end_col}"
        + (f", {plate_col}" if plate_col else "")
        + f" FROM `{schema}`.`{table}` WHERE {inst_col} IN ({placeholders}) AND ({start_col} <= %s AND {end_col} >= %s)"
    )
    args = instruments + [end_max, start_min]
    cur.execute(q, args)
    rt_map = {}
    for row in cur.fetchall():
        if plate_col:
            inst, s, e, pid = row
        else:
            inst, s, e = row
            pid = None
        def to_dt(x):
            if isinstance(x, str):
                return parse_iso8601(x) or dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
            if isinstance(x, dt.datetime):
                return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
            return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        sdt = to_dt(s)
        edt = to_dt(e)
        rt_map.setdefault(str(inst), []).append((sdt, edt, (str(pid) if pid is not None else "")))
    cur.close()
    return rt_map


def fetch_assay_tracking(cnx, db: str, track_tbl: Tuple[str, str], methods: List[str], time_min: dt.datetime, time_max: dt.datetime) -> Dict[str, str]:
    schema, table = track_tbl
    cols = get_columns(cnx, schema, table)
    method_col = pick_first(cols, ["method", "method_name", "protocol", "assay_method"]) or None
    assay_col = pick_first(cols, ["assay", "test", "panel"]) or None
    plate_col = pick_first(cols, ["plate_id", "plate", "batch", "lot"]) or None
    ts_col = pick_first(cols, ["ts", "timestamp", "time", "created_at", "updated_at", "event_time", "dt", "utc_time"]) or None
    if not assay_col or not ts_col:
        return {}

    cur = cnx.cursor()
    # If method column exists, filter by known methods; else just time window.
    if method_col:
        placeholders = ",".join(["%s"] * len(methods)) or "%s"
        q = (
            f"SELECT {method_col}, {assay_col} FROM `{schema}`.`{table}` "
            f"WHERE {ts_col} BETWEEN %s AND %s AND {method_col} IN ({placeholders})"
        )
        args = [time_min, time_max] + (methods if methods else [""])
    else:
        q = (
            f"SELECT {assay_col}, {ts_col} FROM `{schema}`.`{table}` "
            f"WHERE {ts_col} BETWEEN %s AND %s"
        )
        args = [time_min, time_max]
    cur.execute(q, args)
    mapping: Dict[str, str] = {}
    if method_col:
        for m, a in cur.fetchall():
            mapping[str(m)] = str(a)
    else:
        # Without method key, return a single assay for all (best-effort)
        rows = cur.fetchall()
        if rows:
            # pick the most frequent assay
            from collections import Counter
            cnt = Counter([str(r[0]) for r in rows])
            if cnt:
                mapping["__GLOBAL__"] = cnt.most_common(1)[0][0]
    cur.close()
    return mapping


def main() -> int:
    args = parse_args()
    if not os.path.exists(args.input_csv):
        print(f"Input CSV not found: {args.input_csv}", file=sys.stderr)
        return 2

    rows = load_rows(args.input_csv, limit=args.limit)
    if not rows:
        print("No rows to process; exiting.")
        # still write out an empty enriched CSV with header
        save_rows(args.out_csv, rows, [])
        return 0

    # Pre-seed enrichment columns before any DB work. This guarantees that the
    # output schema is stable in skip-db mode and during transient DB failures.
    enrich_cols = [
        "instrument_status_start",
        "occupied_during_run",
        "assay",
        "assay_guess",
        "plate_id",
        "enrich_source_db",
    ]
    for r in rows:
        for c in enrich_cols:
            r.setdefault(c, "")

    dsn = {} if args.skip_db else (parse_dsn(args.dsn_file) if (args.dsn_file and os.path.exists(args.dsn_file)) else {})
    aliases = load_aliases(args.aliases_file)
    database = args.database or dsn.get("DATABASE") or dsn.get("DB")

    machines = sorted({(r.get("machine") or "").strip() for r in rows if (r.get("machine") or "").strip()})
    # Machine aliases let us translate trace-native machine IDs like H7 into
    # whichever instrument names or serials the database happens to use.
    machine_to_instruments = {}
    for m in machines:
        vals = aliases.get(m, [])
        # also support m without H prefix (e.g., '14')
        if not vals and m.upper().startswith('H'):
            vals = aliases.get(m.upper(), [])
        machine_to_instruments[m] = [v for v in vals if v]
    # Query a single combined instrument set once, then fan back out per row.
    db_instruments = sorted({inst for lst in machine_to_instruments.values() for inst in lst if inst})
    methods = sorted({(r.get("method") or "").strip() for r in rows if (r.get("method") or "").strip()})
    # Bound the DB queries to the observed run window so enrichment stays fast
    # and does not scan unrelated historical data.
    start_times: List[dt.datetime] = []
    end_times: List[dt.datetime] = []
    for r in rows:
        s = parse_iso8601(r.get("start_utc", ""))
        e = parse_iso8601(r.get("end_utc", ""))
        if s:
            start_times.append(s)
        if e:
            end_times.append(e)
    if start_times and end_times:
        start_min = min(start_times)
        end_max = max(end_times)
    else:
        # fallback to now window
        now = dt.datetime.now(dt.timezone.utc)
        start_min = now - dt.timedelta(days=30)
        end_max = now

    status_map: Dict[str, List[Tuple[dt.datetime, str]]] = {}
    status_intervals: Dict[str, List[Tuple[dt.datetime, dt.datetime, str]]] = {}
    occ_map: Dict[str, List[Tuple[dt.datetime, dt.datetime]]] = {}
    runtime_map: Dict[str, List[Tuple[dt.datetime, dt.datetime, str]]] = {}
    assay_map: Dict[str, str] = {}

    cnx = None
    if args.skip_db:
        print("INFO: --skip-db set; writing output with blank enrichment fields.")
    elif database:
        try:
            cnx = connect_mysql_readonly(dsn, database)
            # Auto-detect the relevant tables because lab schemas have drifted
            # over time and naming is not stable across environments.
            status_tbl = resolve_table(cnx, database, args.status_table, [
                "instrument_status",
                "instrument_statuses",
                "instrument_status_log",
                "instrument_status_history",
            ])
            occ_tbl = resolve_table(cnx, database, args.occupation_table, [
                "instrument_ocupation",
                "instrument_occupation",
                "instrument_usage",
            ])
            track_tbl = resolve_table(cnx, database, args.tracking_table, [
                "vibrant_test_tracking",
                "test_tracking",
                "assay_tracking",
            ])

            if status_tbl and db_instruments:
                # Prefer interval-style status data because it gives direct
                # overlap semantics. Fall back to point-in-time status logs.
                status_intervals = fetch_status_intervals(cnx, database, status_tbl, db_instruments, start_min, end_max)
                if not status_intervals:
                    status_map = fetch_status_maps(cnx, database, status_tbl, db_instruments, start_min, end_max)
            if occ_tbl and db_instruments:
                occ_map = fetch_occupation_intervals(cnx, database, occ_tbl, db_instruments, start_min, end_max)
            elif status_intervals:
                # Fall back: use status intervals as occupation windows
                for inst, lst in status_intervals.items():
                    occ_map[inst] = [(s, e) for (s, e, _st) in lst]
            rt_tbl = resolve_table(cnx, database, args.runtime_table, [
                "instrument_runtime",
                "instrument_run_time",
            ])
            if rt_tbl and db_instruments:
                runtime_map = fetch_runtime_intervals(cnx, database, rt_tbl, db_instruments, start_min, end_max)
            if track_tbl:
                assay_map = fetch_assay_tracking(cnx, database, track_tbl, methods, start_min, end_max)
        except Exception as e:
            # DB failures should degrade enrichment quality, not pipeline
            # availability. The pre-seeded columns stay blank in that case.
            print(f"WARNING: Enrichment skipped due to DB error: {e}", file=sys.stderr)
        finally:
            try:
                if cnx is not None:
                    cnx.rollback()  # rollback read-only txn (no writes)
                    cnx.close()
            except Exception:
                pass
    else:
        print("INFO: No primary database specified; skipping status/occupation enrichment.")

    # Optional assay-name loading is used only for a best-effort guess. It is
    # intentionally separate from the core enrichment path so failure here does
    # not block status/occupation/plate joins.
    tests_db = None if args.skip_db else (args.tests_database or dsn.get("TESTS_DATABASE") or None)
    test_names: List[str] = []
    if tests_db:
        try:
            cnx2 = connect_mysql_readonly(dsn, tests_db)
            cur = cnx2.cursor()
            # Prefer well_plate_info, fallback to test_list if present
            try:
                cur.execute("SELECT DISTINCT test_name FROM `" + tests_db + "`.`well_plate_info` WHERE test_name IS NOT NULL AND test_name <> ''")
                test_names = [r[0] for r in cur.fetchall()]
            except Exception:
                try:
                    cur.execute("SELECT DISTINCT test_name FROM `" + tests_db + "`.`test_list` WHERE test_name IS NOT NULL AND test_name <> ''")
                    test_names = [r[0] for r in cur.fetchall()]
                except Exception:
                    test_names = []
            cur.close(); cnx2.rollback(); cnx2.close()
        except Exception as e:
            print(f"WARNING: Failed to load test names from {tests_db}: {e}", file=sys.stderr)

    # Row assignment is intentionally conservative: if we cannot match a field
    # confidently, leave it blank rather than inventing a hard claim.
    for r in rows:
        m = (r.get("machine") or "").strip()
        inst_candidates = machine_to_instruments.get(m, [])
        sdt = parse_iso8601(r.get("start_utc", "")) or start_min
        edt = parse_iso8601(r.get("end_utc", "")) or sdt

        # instrument status at start
        status_val = ""
        for inst in inst_candidates:
            # prefer interval-style
            intervals = status_intervals.get(inst) or []
            found = None
            for s, e, st in intervals:
                if s <= sdt <= e:
                    found = st
                    break
            if not found:
                # fall back to point-style
                status_list = status_map.get(inst) or []
                cand = ""
                for ts, st in reversed(status_list):
                    if ts <= sdt:
                        cand = st
                        break
                if not cand and status_list:
                    cand = status_list[0][1]
                found = cand
            if found:
                status_val = found
                break
        r["instrument_status_start"] = status_val

        # Occupation is modeled as any overlap between the run window and an
        # instrument occupation/status interval.
        occupied = 0
        for inst in inst_candidates:
            intervals = occ_map.get(inst) or []
            for s, e in intervals:
                if s <= edt and e >= sdt:
                    occupied = 1
                    break
            if occupied:
                break
        r["occupied_during_run"] = str(occupied)

        # Assay uses a direct method mapping when available.
        method = (r.get("method") or "").strip()
        a = assay_map.get(method) or assay_map.get("__GLOBAL__", "")
        r["assay"] = a

        # assay_guess is weaker than assay. It is a token match against known
        # test names so analysts can inspect likely candidates later.
        guess = ""
        if test_names:
            import re
            tokens = set([t for t in re.split(r"[^A-Za-z0-9]+", (r.get("name") or "") + " " + method) if t])
            candidates = [t for t in test_names if t and t in tokens]
            if candidates:
                guess = ",".join(sorted(set(candidates)))
        r["assay_guess"] = guess

        # plate_id is taken from the closest overlapping runtime interval.
        plate = r.get("plate_id", "")
        if not plate and inst_candidates:
            for inst in inst_candidates:
                entries = runtime_map.get(inst) or []
                best = None
                for s, e, pid in entries:
                    if s <= edt and e >= sdt and pid:
                        # choose the closest overlapping interval by start proximity
                        delta = abs((s - sdt).total_seconds())
                        if best is None or delta < best[0]:
                            best = (delta, pid)
                if best:
                    plate = best[1]
                    break
        r["plate_id"] = plate

        r["enrich_source_db"] = database or ""

    # Preserve the original field order and append enrichment fields in a fixed
    # position so downstream imports do not churn when enrichment is disabled.
    base_fields = list(rows[0].keys())
    # Ensure enrichment columns are at the end in stable order
    for c in enrich_cols:
        if c in base_fields:
            continue
        base_fields.append(c)
    save_rows(args.out_csv, rows, base_fields)
    print(f"Wrote enriched CSV: {args.out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
