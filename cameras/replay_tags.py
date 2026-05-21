#!/usr/bin/env python3
"""Trace-derived replay tag helpers for local and staged replay metadata."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable


TRACE_TAGS_VERSION = "replay-tags.v1"
BARCODE_RE = re.compile(r"Barcode for (?P<slot>NO\d+) pillar plate is:\s*(?P<barcode>[A-Za-z0-9._-]+)", re.IGNORECASE)
ABORT_TEXT_PATTERNS = (
    "step aborted",
    "abort method",
    "abort command",
    ":abort",
)


def _normalize_tag(
    *,
    key: str,
    value: str,
    label: str,
    scope: str = "visible",
    source: str = "trace",
    metadata: dict | None = None,
) -> dict:
    return {
        "key": key,
        "value": str(value),
        "label": label,
        "scope": scope,
        "source": source,
        "metadata": metadata or {},
    }


def _load_trace_lines(trace_path: Path | None) -> list[str]:
    if trace_path is None or not trace_path.exists() or not trace_path.is_file():
        return []
    with trace_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        return [line.rstrip("\r\n") for line in handle if line.strip()]


def build_search_text(tags: list[dict], summary: dict) -> str:
    terms: list[str] = []
    for tag in tags:
        terms.append(str(tag.get("key") or ""))
        terms.append(str(tag.get("value") or ""))
        for value in (tag.get("metadata") or {}).values():
            terms.append(str(value))
    terms.append(str(summary.get("outcome") or ""))
    terms.extend(summary.get("pillar_plate_barcodes") or [])
    normalized = " ".join(term.strip().lower() for term in terms if str(term).strip())
    return " ".join(normalized.split())


def derive_run_tags(trace_path: Path | None, *, log_fn: Callable[[str], None] | None = None) -> dict:
    """Extract a narrow set of replay-search tags from a Hamilton trace."""
    lines = _load_trace_lines(trace_path)
    tags: list[dict] = []
    if not lines:
        summary = {
            "version": TRACE_TAGS_VERSION,
            "source": "trace",
            "trace_path": str(trace_path.resolve()) if trace_path else "",
            "tag_count": 0,
            "primary_barcode": "",
            "pillar_plate_barcodes": [],
            "has_error": False,
            "has_abort": False,
            "error_count": 0,
            "abort_count": 0,
            "outcome": "unknown",
            "search_text": "",
        }
        if log_fn:
            trace_label = str(trace_path.resolve()) if trace_path else ""
            log_fn(
                f"[replay-tags] trace={trace_label or '<missing>'} lines=0 outcome=unknown primary_barcode= barcodes=0 errors=0 aborts=0"
            )
        return {"version": TRACE_TAGS_VERSION, "tags": tags, "summary": summary, "search_text": ""}

    seen_barcodes: set[str] = set()
    ordered_barcodes: list[tuple[str, str]] = []
    error_count = 0
    abort_count = 0

    for line in lines:
        barcode_match = BARCODE_RE.search(line)
        if barcode_match:
            slot = barcode_match.group("slot").upper()
            barcode = barcode_match.group("barcode").strip()
            if barcode and barcode not in seen_barcodes:
                ordered_barcodes.append((slot, barcode))
                seen_barcodes.add(barcode)
        lowered = line.lower()
        if " - error" in lowered or "complete with error" in lowered:
            error_count += 1
        if any(pattern in lowered for pattern in ABORT_TEXT_PATTERNS):
            abort_count += 1

    for slot, barcode in ordered_barcodes:
        tags.append(
            _normalize_tag(
                key="pillar_plate_barcode",
                value=barcode,
                label="Pillar plate barcode",
                metadata={"slot": slot},
            )
        )

    has_error = error_count > 0
    has_abort = abort_count > 0
    outcome = "aborted" if has_abort else "error" if has_error else "ok"
    tags.append(_normalize_tag(key="run_outcome", value=outcome, label="Run outcome"))
    tags.append(_normalize_tag(key="has_error", value=str(has_error).lower(), label="Has trace error"))
    tags.append(_normalize_tag(key="has_abort", value=str(has_abort).lower(), label="Has trace abort"))
    tags.append(_normalize_tag(key="trace_error_count", value=str(error_count), label="Trace error count", scope="internal"))
    tags.append(_normalize_tag(key="trace_abort_count", value=str(abort_count), label="Trace abort count", scope="internal"))

    summary = {
        "version": TRACE_TAGS_VERSION,
        "source": "trace",
        "trace_path": str(trace_path.resolve()) if trace_path else "",
        "tag_count": len(tags),
        "primary_barcode": ordered_barcodes[0][1] if ordered_barcodes else "",
        "pillar_plate_barcodes": [barcode for _slot, barcode in ordered_barcodes],
        "has_error": has_error,
        "has_abort": has_abort,
        "error_count": error_count,
        "abort_count": abort_count,
        "outcome": outcome,
    }
    summary["search_text"] = build_search_text(tags, summary)
    if log_fn:
        log_fn(
            "[replay-tags] "
            f"trace={summary['trace_path'] or '<missing>'} "
            f"lines={len(lines)} "
            f"outcome={summary['outcome']} "
            f"primary_barcode={summary['primary_barcode'] or '-'} "
            f"barcodes={len(summary['pillar_plate_barcodes'])} "
            f"errors={summary['error_count']} aborts={summary['abort_count']}"
        )
    return {
        "version": TRACE_TAGS_VERSION,
        "tags": tags,
        "summary": summary,
        "search_text": summary["search_text"],
    }


def serialize_tags(tags: list[dict]) -> str:
    return json.dumps(tags, separators=(",", ":"), ensure_ascii=True)


def serialize_summary(summary: dict) -> str:
    return json.dumps(summary, separators=(",", ":"), ensure_ascii=True)
