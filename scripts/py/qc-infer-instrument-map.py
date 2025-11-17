#!/usr/bin/env python3
"""
qc-infer-instrument-map.py

Infer mapping from Hamilton bench machines (e.g., H6) to DB instrument IDs
by cross-referencing run summary time windows with runtime intervals in MySQL.

Safety: opens a READ ONLY transaction; executes only SELECT statements.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from typing import Dict, List, Optional, Tuple


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Infer bench->DB instrument mapping from time overlaps")
    p.add_argument("--input-csv", required=True, help="Run summaries CSV (from qc-aggregate-summaries.ps1)")
    p.add_argument("--dsn-file", default="config/mysql_labsite.dsn", help="Path to DSN (key=value)")
    p.add_argument("--runtime-table", default="operation_statistic.instrument_runtime", help="schema.table for runtime intervals")
    p.add_argument("--uploads-table", default="vibrant_automation.ms_uploaded_raw_files", help="Optional schema.table for uploaded raw files with instrument_id + datetime")
    p.add_argument("--status-table", default="operation_data.instrument_status", help="Optional schema.table for instrument status intervals (Instrument_name, start_time, end_time)")
    p.add_argument("--machine", required=True, help="Bench machine to analyze (e.g., H6)")
    p.add_argument("--limit-runs", type=int, default=20, help="Max runs to sample for inference")
    p.add_argument("--code-hint", action="append", default=None, help="Hint code(s) to fuzzy-match in instrument names (can repeat)")
    p.add_argument("--min-overlap-sec", type=int, default=60, help="Minimum overlap seconds to count an interval")
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
        if s.endswith("Z"):
            return dt.datetime.fromisoformat(s[:-1] + "+00:00")
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def connect_mysql_readonly(dsn: Dict[str, str]):
    import mysql.connector  # type: ignore
    cnx = mysql.connector.connect(
        host=dsn.get("SERVER") or dsn.get("HOST") or "localhost",
        port=int(dsn.get("PORT", "3306")),
        user=dsn.get("USER") or dsn.get("UID") or "",
        password=dsn.get("PASSWORD") or dsn.get("PWD") or "",
        autocommit=False,
    )
    try:
        cur = cnx.cursor(); cur.execute("SET SESSION TRANSACTION READ ONLY"); cnx.start_transaction(readonly=True); cur.close()
    except Exception:
        pass
    return cnx


def load_runs(path: str, machine: str, limit_runs: int) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if (r.get("machine") or "").strip().upper() != machine.upper():
                continue
            s = parse_iso8601(r.get("start_utc", ""))
            e = parse_iso8601(r.get("end_utc", ""))
            if s and e and e >= s:
                rows.append(r)
            if len(rows) >= limit_runs:
                break
    return rows


def infer_mapping(cnx, runtime_schema: str, runtime_table: str, runs: List[Dict[str, str]], min_overlap_sec: int, uploads: Optional[Tuple[str,str]] = None, status: Optional[Tuple[str,str]] = None, code_hints: Optional[List[str]] = None):
    cur = cnx.cursor()
    scores: Dict[str, Dict[str, float]] = {}
    samples: Dict[str, List[Tuple[str, str, str]]] = {}
    for r in runs:
        run_id = r.get("run_id", "") or f"{r.get('machine','')}:{r.get('name','')}"
        sdt = parse_iso8601(r.get("start_utc", ""))
        edt = parse_iso8601(r.get("end_utc", ""))
        if not sdt or not edt:
            continue
        q = (
            f"SELECT instrument, start_time, end_time, plate_id "
            f"FROM `{runtime_schema}`.`{runtime_table}` "
            f"WHERE start_time <= %s AND end_time >= %s"
        )
        cur.execute(q, (edt, sdt))
        for inst, s, e, pid in cur.fetchall():
            if not inst:
                inst = "(null)"
            # normalize datetimes
            if isinstance(s, str):
                s = parse_iso8601(s)
            if isinstance(e, str):
                e = parse_iso8601(e)
            if isinstance(s, dt.datetime) and s.tzinfo is None:
                s = s.replace(tzinfo=dt.timezone.utc)
            if isinstance(e, dt.datetime) and e.tzinfo is None:
                e = e.replace(tzinfo=dt.timezone.utc)
            if not isinstance(s, dt.datetime) or not isinstance(e, dt.datetime):
                continue
            overlap = (min(edt, e) - max(sdt, s)).total_seconds()
            if overlap < min_overlap_sec:
                continue
            inst_s = str(inst)
            rec = scores.setdefault(inst_s, {"overlap_sec": 0.0, "hits": 0.0})
            rec["overlap_sec"] += overlap
            rec["hits"] += 1
            if len(samples.setdefault(inst_s, [])) < 3:
                samples[inst_s].append((run_id, s.isoformat(), e.isoformat()))
        # Fallback/events: uploaded raw files (point timestamps tied to instrument_id)
        if uploads:
            uschema, utable = uploads
            # Convert run window to local time 'YYYYMMDDHHMMSS' string to match varchar storage
            s_loc = sdt.astimezone().strftime("%Y%m%d%H%M%S")
            e_loc = edt.astimezone().strftime("%Y%m%d%H%M%S")
            q2 = (
                f"SELECT instrument_id, datetime FROM `{uschema}`.`{utable}` WHERE `datetime` BETWEEN %s AND %s"
            )
            cur.execute(q2, (s_loc, e_loc))
            for inst, t in cur.fetchall():
                if not inst:
                    inst = "(null)"
                inst_s = str(inst)
                rec = scores.setdefault(inst_s, {"overlap_sec": 0.0, "hits": 0.0})
                rec["hits"] += 1
                if len(samples.setdefault(inst_s, [])) < 3:
                    samples[inst_s].append((run_id, str(t), str(t)))
        # Status intervals (Instrument_name, start_time, end_time): treat any overlap as a hit, weight by overlap seconds
        if status:
            sschema, stable = status
            q3 = (
                f"SELECT Instrument_name, start_time, end_time, status FROM `{sschema}`.`{stable}` WHERE start_time <= %s AND end_time >= %s"
            )
            try:
                cur.execute(q3, (edt, sdt))
                for inst, s, e, st in cur.fetchall():
                    if isinstance(s, str): s = parse_iso8601(s)
                    if e is None:
                        e = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
                    elif isinstance(e, str):
                        e = parse_iso8601(e)
                    if isinstance(s, dt.datetime) and s.tzinfo is None: s = s.replace(tzinfo=dt.timezone.utc)
                    if isinstance(e, dt.datetime) and e.tzinfo is None: e = e.replace(tzinfo=dt.timezone.utc)
                    if not isinstance(s, dt.datetime) or not isinstance(e, dt.datetime):
                        continue
                    overlap = (min(edt, e) - max(sdt, s)).total_seconds()
                    if overlap < min_overlap_sec:
                        continue
                    inst_s = str(inst)
                    rec = scores.setdefault(inst_s, {"overlap_sec": 0.0, "hits": 0.0})
                    rec["overlap_sec"] += overlap
                    rec["hits"] += 1
                    if len(samples.setdefault(inst_s, [])) < 3:
                        samples[inst_s].append((run_id, s.isoformat(), e.isoformat()))
            except Exception:
                pass
    cur.close()
    # Boost scores for code hints if provided
    if code_hints:
        hints = [h.lower() for h in code_hints if h]
        for inst, rec in scores.items():
            low = inst.lower()
            if any(h in low for h in hints):
                # Add a generous bonus to push hinted names up
                rec["overlap_sec"] += 1e9
    # Sort by total overlap (desc), then hits
    ranked = sorted(scores.items(), key=lambda kv: (kv[1]["overlap_sec"], kv[1]["hits"]), reverse=True)
    return ranked, samples


def main() -> int:
    args = parse_args()
    dsn = parse_dsn(args.dsn_file)
    runs = load_runs(args.input_csv, args.machine, args.limit_runs)
    if not runs:
        print("No runs found with valid times for the selected machine.")
        return 0
    if "." in args.runtime_table:
        schema, table = args.runtime_table.split(".", 1)
    else:
        schema, table = "operation_statistic", args.runtime_table
    uploads: Optional[Tuple[str,str]] = None
    if args.uploads_table:
        if "." in args.uploads_table:
            us, ut = args.uploads_table.split(".", 1)
        else:
            us, ut = "vibrant_automation", args.uploads_table
        uploads = (us, ut)
    status: Optional[Tuple[str,str]] = None
    if args.status_table:
        if "." in args.status_table:
            ss, st = args.status_table.split(".", 1)
        else:
            ss, st = "operation_data", args.status_table
        status = (ss, st)
    try:
        cnx = connect_mysql_readonly(dsn)
    except Exception as e:
        print(f"ERROR: DB connect failed: {e}", file=sys.stderr)
        return 2
    ranked, samples = infer_mapping(cnx, schema, table, runs, args.min_overlap_sec, uploads, status, args.code_hint)
    try:
        cnx.rollback(); cnx.close()
    except Exception:
        pass
    if not ranked:
        print("No overlapping runtime intervals found; unable to infer mapping.")
        return 0
    print(f"Candidate instruments for {args.machine} (top 10):")
    for inst, rec in ranked[:10]:
        print(f"- {inst}: overlap_sec={int(rec['overlap_sec'])}, hits={int(rec['hits'])}")
        for (run_id, s, e) in samples.get(inst, []):
            print(f"    sample: {run_id} ~ [{s} .. {e}]")
    best = ranked[0][0]
    print(f"\nSuggested mapping: {args.machine} -> {best}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
