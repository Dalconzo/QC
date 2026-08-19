#!/usr/bin/env python3
"""Trace-derived replay tag helpers for local and staged replay metadata."""
from __future__ import annotations

import json
import re
from pathlib import Path


TRACE_TAGS_VERSION = "replay-tags.v2"
SLOTTED_BARCODE_RE = re.compile(
    r"Barcode for\s+(?P<slot>NO\d+)\s+pillar plate is:\s*(?P<barcode>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
UNSCOPED_BARCODE_RE = re.compile(r"pillar barcode:\s*(?P<barcode>[A-Za-z0-9._-]+)", re.IGNORECASE)
END_METHOD_COMPLETE_RE = re.compile(r"SYSTEM\s*:\s*End method\s*-\s*complete\s*;", re.IGNORECASE)
ABORT_TEXT_PATTERNS = (
    "step aborted",
    "method has been aborted",
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


def derive_run_tags(trace_path: Path | None) -> dict:
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
            "end_method_complete": False,
            "error_count": 0,
            "abort_count": 0,
            "outcome": "unknown",
            "search_text": "",
        }
        return {"version": TRACE_TAGS_VERSION, "tags": tags, "summary": summary, "search_text": ""}

    ordered_barcode_keys: list[str] = []
    barcode_records: dict[str, dict] = {}
    error_count = 0
    abort_count = 0
    end_method_complete = False

    for line in lines:
        barcode_match = SLOTTED_BARCODE_RE.search(line)
        slot = barcode_match.group("slot").upper() if barcode_match else ""
        if barcode_match is None:
            barcode_match = UNSCOPED_BARCODE_RE.search(line)
        if barcode_match:
            barcode = barcode_match.group("barcode").strip()
            barcode_key = barcode.casefold()
            if barcode and barcode_key not in barcode_records:
                ordered_barcode_keys.append(barcode_key)
                barcode_records[barcode_key] = {"barcode": barcode, "slots": []}
            if barcode and slot:
                record = barcode_records[barcode_key]
                record["barcode"] = barcode
                if slot not in record["slots"]:
                    record["slots"].append(slot)
        lowered = line.lower()
        if " - error" in lowered or "complete with error" in lowered:
            error_count += 1
        if any(pattern in lowered for pattern in ABORT_TEXT_PATTERNS):
            abort_count += 1
        if END_METHOD_COMPLETE_RE.search(line):
            end_method_complete = True

    ordered_barcodes = [barcode_records[key] for key in ordered_barcode_keys]
    for record in ordered_barcodes:
        metadata = {"slots": record["slots"]}
        if record["slots"]:
            metadata["slot"] = record["slots"][0]
        tags.append(
            _normalize_tag(
                key="pillar_plate_barcode",
                value=record["barcode"],
                label="Pillar plate barcode",
                metadata=metadata,
            )
        )

    has_error = error_count > 0
    has_abort = abort_count > 0
    outcome = "aborted" if has_abort else "error" if has_error else "ok" if end_method_complete else "unknown"
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
        "primary_barcode": ordered_barcodes[0]["barcode"] if ordered_barcodes else "",
        "pillar_plate_barcodes": [record["barcode"] for record in ordered_barcodes],
        "has_error": has_error,
        "has_abort": has_abort,
        "end_method_complete": end_method_complete,
        "error_count": error_count,
        "abort_count": abort_count,
        "outcome": outcome,
    }
    summary["search_text"] = build_search_text(tags, summary)
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
