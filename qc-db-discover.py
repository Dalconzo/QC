#!/usr/bin/env python3
"""
qc-db-discover.py

Read-only discovery of MySQL schemas and tables relevant to Hamilton tracking.
Prints candidate tables for:
 - instrument status history
 - instrument occupation/usage intervals
 - test/assay tracking (method, assay, plate)

Safety: opens a READ ONLY transaction and only issues SELECTs against information_schema and a small LIMIT on sample queries.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional, Tuple


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only discovery of Hamilton tracking tables in MySQL")
    p.add_argument("--dsn-file", default="mysql_labsite.dsn", help="Path to DSN (key=value) with SERVER, PORT, USER, PASSWORD")
    p.add_argument("--schemas", nargs="*", default=None, help="Limit discovery to these schemas (optional)")
    p.add_argument("--sample-limit", type=int, default=3, help="Sample rows to print per candidate table")
    p.add_argument("--max-tables", type=int, default=5, help="Max tables to display per category")
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


def connect_readonly(dsn: Dict[str, str]):
    import mysql.connector  # type: ignore

    params = {
        "host": dsn.get("SERVER") or dsn.get("HOST") or "localhost",
        "port": int(dsn.get("PORT", "3306")),
        "user": dsn.get("USER") or dsn.get("UID") or "",
        "password": dsn.get("PASSWORD") or dsn.get("PWD") or "",
        "connection_timeout": 5,
        "autocommit": False,
    }
    cnx = mysql.connector.connect(**params)
    try:
        cur = cnx.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cnx.start_transaction(readonly=True)
        cur.close()
    except Exception:
        pass
    return cnx


INTERNAL_SCHEMAS = {"information_schema", "performance_schema", "mysql", "sys"}


def fetch_schemas(cnx, allow: Optional[List[str]] = None) -> List[str]:
    cur = cnx.cursor()
    if allow:
        placeholders = ",".join(["%s"] * len(allow))
        cur.execute(
            f"SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ({placeholders}) ORDER BY SCHEMA_NAME",
            allow,
        )
    else:
        cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME")
    schemas = [r[0] for r in cur.fetchall() if r[0] not in INTERNAL_SCHEMAS]
    cur.close()
    return schemas


def fetch_columns(cnx, schema: str, table: str) -> List[Tuple[str, str]]:
    cur = cnx.cursor()
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
        (schema, table),
    )
    cols = [(r[0], r[1]) for r in cur.fetchall()]
    cur.close()
    return cols


def score_table(columns: List[str]) -> Dict[str, int]:
    cols_l = {c.lower() for c in columns}
    def has_any(names: List[str]) -> bool:
        return any(n in cols_l for n in names)

    scores = {"status": 0, "occupation": 0, "tracking": 0}

    # Status needs machine + ts + status
    if has_any(["machine", "machine_name", "instrument", "instrument_name", "host", "hostname"]) and \
       has_any(["ts", "timestamp", "time", "created_at", "updated_at", "event_time", "dt", "utc_time"]) and \
       has_any(["status", "state", "instrument_status", "value"]):
        scores["status"] = 3

    # Occupation needs machine + start + end
    if has_any(["machine", "machine_name", "instrument", "instrument_name", "host", "hostname"]) and \
       has_any(["start_ts", "start_time", "start", "begin_ts", "begin_time"]) and \
       has_any(["end_ts", "end_time", "end", "stop_ts", "stop_time"]):
        scores["occupation"] = 3

    # Tracking needs method and assay/plate/test
    if has_any(["method", "method_name", "protocol", "assay_method"]) and \
       has_any(["assay", "test", "panel", "plate_id", "plate", "batch", "lot"]):
        scores["tracking"] = 3

    return scores


def sample_rows(cnx, schema: str, table: str, columns: List[str], limit: int = 3) -> List[Dict[str, str]]:
    # Heuristic order by ts desc if present
    import mysql.connector
    ts_cols = [c for c in columns if c.lower() in ("ts", "timestamp", "time", "created_at", "updated_at", "event_time", "dt", "utc_time")]
    order = f" ORDER BY `{ts_cols[0]}` DESC" if ts_cols else ""
    cur = cnx.cursor(dictionary=True)
    try:
        cur.execute(f"SELECT * FROM `{schema}`.`{table}`{order} LIMIT %s", (limit,))
        rows = cur.fetchall()
        return rows
    except mysql.connector.Error:
        return []
    finally:
        cur.close()


def main() -> int:
    args = parse_args()
    dsn = parse_dsn(args.dsn_file)
    try:
        import mysql.connector  # noqa: F401
    except Exception:
        print("ERROR: mysql-connector-python not installed.", file=sys.stderr)
        return 2

    try:
        cnx = connect_readonly(dsn)
    except Exception as e:
        print(f"ERROR: failed to connect: {e}", file=sys.stderr)
        return 2

    schemas = fetch_schemas(cnx, args.schemas)
    print(f"Discovered schemas: {', '.join(schemas) or '(none)'}")

    candidates_status = []
    candidates_occ = []
    candidates_track = []

    cur = cnx.cursor()
    # Fetch all tables in selected schemas
    placeholders = ",".join(["%s"] * len(schemas)) if schemas else "%s"
    if schemas:
        cur.execute(
            f"SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES WHERE TABLE_TYPE='BASE TABLE' AND TABLE_SCHEMA IN ({placeholders}) ORDER BY TABLE_SCHEMA, TABLE_NAME",
            schemas,
        )
    else:
        cur.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES WHERE TABLE_TYPE='BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME"
        )
    all_tables = [(r[0], r[1]) for r in cur.fetchall() if r[0] not in INTERNAL_SCHEMAS]
    cur.close()

    for schema, table in all_tables:
        cols = [c for c, _ in fetch_columns(cnx, schema, table)]
        scores = score_table(cols)
        if scores["status"]:
            candidates_status.append((schema, table, cols))
        if scores["occupation"]:
            candidates_occ.append((schema, table, cols))
        if scores["tracking"]:
            candidates_track.append((schema, table, cols))

    def print_candidates(title: str, items: List[Tuple[str, str, List[str]]], max_items: int):
        print(f"\n=== {title} ===")
        if not items:
            print("(none)")
            return
        shown = 0
        for schema, table, cols in items[:max_items]:
            print(f"- {schema}.{table} : columns=({', '.join(cols)})")
            rows = sample_rows(cnx, schema, table, cols, limit=args.sample_limit)
            for i, r in enumerate(rows):
                # show first ~8 columns for readability
                keys = list(r.keys())[:8]
                preview = {k: r[k] for k in keys}
                print(f"  sample[{i}]: {preview}")
            shown += 1
        if len(items) > max_items:
            print(f"  ... and {len(items)-max_items} more")

    print_candidates("Instrument Status", candidates_status, args.max_tables)
    print_candidates("Instrument Occupation", candidates_occ, args.max_tables)
    print_candidates("Assay/Test Tracking", candidates_track, args.max_tables)

    try:
        cnx.rollback()
        cnx.close()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

