#!/usr/bin/env python3
"""
trace_replay.py

Helpers that derive replay-friendly chapter and segment metadata from Hamilton
trace files.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path


TRACE_LINE_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})> ?(?P<body>.*)$")
DEFAULT_IDLE_GAP_SEC = 120.0
EXPLICIT_IDLE_MIN_GAP_SEC = 30.0
CHAPTER_DEDUP_SEC = 15.0

EXPLICIT_IDLE_PATTERNS = (
    re.compile(r"\bstarting timer:\s*(?P<name>[\w-]+)", re.IGNORECASE),
    re.compile(r"\bincubat(?:e|ion|ing)?\b", re.IGNORECASE),
    re.compile(r"\bwait(?:ing)?\b", re.IGNORECASE),
    re.compile(r"\bdelay\b", re.IGNORECASE),
    re.compile(r"\bpause\b", re.IGNORECASE),
    re.compile(r"\bhold\b", re.IGNORECASE),
)

CHAPTER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bAnalyze method - start\b", re.IGNORECASE), "Analyze method"),
    (re.compile(r"\bStart method - start\b", re.IGNORECASE), "Start method"),
    (re.compile(r"\bExecute method - start\b", re.IGNORECASE), "Execute method"),
    (re.compile(r"\bCustom Dialog - start\b", re.IGNORECASE), "Operator dialog"),
    (re.compile(r"\bAbort method - complete\b", re.IGNORECASE), "Abort method"),
    (re.compile(r"\bFile checksum - written\b", re.IGNORECASE), "Finalize trace"),
)


@dataclass(frozen=True)
class TraceLine:
    """One timestamped Hamilton trace line."""

    index: int
    stamp: dt.datetime
    elapsed_sec: float
    line: str
    body: str


def parse_trace_lines(trace_path: Path) -> list[TraceLine]:
    """Parse Hamilton trace lines into timestamped records."""
    lines: list[TraceLine] = []
    first_stamp: dt.datetime | None = None
    with trace_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            match = TRACE_LINE_RE.match(line)
            if not match:
                continue
            stamp = dt.datetime.strptime(match.group("stamp"), "%Y-%m-%d %H:%M:%S")
            if first_stamp is None:
                first_stamp = stamp
            elapsed_sec = max(0.0, (stamp - first_stamp).total_seconds())
            lines.append(
                TraceLine(
                    index=len(lines),
                    stamp=stamp,
                    elapsed_sec=elapsed_sec,
                    line=line,
                    body=match.group("body").strip(),
                )
            )
    return lines


def _format_idle_label(current: TraceLine, following: TraceLine) -> str:
    for candidate in (current.body, following.body):
        for pattern in EXPLICIT_IDLE_PATTERNS:
            match = pattern.search(candidate)
            if not match:
                continue
            name = (match.groupdict().get("name") or "").strip(" _-")
            if name:
                return f"Idle: {name}"
            if "incubat" in candidate.lower():
                return "Idle: incubation"
            if "wait" in candidate.lower():
                return "Idle: wait"
            if "delay" in candidate.lower():
                return "Idle: delay"
            if "pause" in candidate.lower():
                return "Idle: pause"
            if "hold" in candidate.lower():
                return "Idle: hold"
    return "Idle window"


def classify_phase_label(line: TraceLine | None) -> str:
    """Return a readable phase label for a trace line."""
    if line is None:
        return "Run activity"
    for pattern, label in CHAPTER_PATTERNS:
        if pattern.search(line.body):
            return label
    idle_hint = _format_idle_label(line, line)
    if idle_hint != "Idle window":
        return idle_hint
    body = line.body
    if " - " in body:
        return body.split(" - ", 1)[0].strip() or "Run activity"
    if ":" in body:
        return body.split(":", 1)[0].strip() or "Run activity"
    shortened = body[:80].strip()
    return shortened or "Run activity"


def _is_explicit_idle_transition(current: TraceLine, following: TraceLine) -> bool:
    current_body = current.body.lower()
    next_body = following.body.lower()
    for pattern in EXPLICIT_IDLE_PATTERNS:
        if pattern.search(current.body) or pattern.search(following.body):
            return True
    return "timer_" in current_body or "timer_" in next_body


def _append_segment(
    items: list[dict],
    *,
    kind: str,
    start_offset_sec: float,
    stop_offset_sec: float,
    phase_label: str,
    phase_line: TraceLine | None,
) -> None:
    duration_sec = round(stop_offset_sec - start_offset_sec, 3)
    if duration_sec <= 0:
        return
    items.append(
        {
            "segment_id": f"{kind}-{len(items) + 1:03d}",
            "kind": kind,
            "start_offset_sec": round(start_offset_sec, 3),
            "stop_offset_sec": round(stop_offset_sec, 3),
            "duration_sec": duration_sec,
            "phase_label": phase_label,
            "phase_source": "trace",
            "source_line_index": phase_line.index if phase_line else None,
            "video_path": "",
            "video_encoding_profile": "source_full_run",
            "is_skipped_by_default": kind == "idle",
        }
    )


def build_trace_replay_summary(
    trace_path: Path,
    *,
    min_idle_gap_sec: float = DEFAULT_IDLE_GAP_SEC,
) -> dict:
    """Derive replay chapters and active/idle windows from one trace."""
    lines = parse_trace_lines(trace_path)
    if not lines:
        return {
            "trace_event_count": 0,
            "trace_started_at_local": "",
            "trace_stopped_at_local": "",
            "trace_duration_sec": 0.0,
            "chapters": [],
            "segments": [],
            "idle_segment_count": 0,
            "active_segment_count": 0,
        }

    total_duration_sec = round(lines[-1].elapsed_sec, 3)
    segments: list[dict] = []
    active_start_sec = 0.0
    active_anchor = lines[0]

    for index in range(len(lines) - 1):
        current = lines[index]
        following = lines[index + 1]
        gap_sec = max(0.0, following.elapsed_sec - current.elapsed_sec)
        explicit_idle = _is_explicit_idle_transition(current, following)
        if gap_sec < min_idle_gap_sec and not (explicit_idle and gap_sec >= EXPLICIT_IDLE_MIN_GAP_SEC):
            continue
        _append_segment(
            segments,
            kind="active",
            start_offset_sec=active_start_sec,
            stop_offset_sec=current.elapsed_sec,
            phase_label=classify_phase_label(active_anchor),
            phase_line=active_anchor,
        )
        _append_segment(
            segments,
            kind="idle",
            start_offset_sec=current.elapsed_sec,
            stop_offset_sec=following.elapsed_sec,
            phase_label=_format_idle_label(current, following),
            phase_line=current,
        )
        active_start_sec = following.elapsed_sec
        active_anchor = following

    _append_segment(
        segments,
        kind="active",
        start_offset_sec=active_start_sec,
        stop_offset_sec=total_duration_sec,
        phase_label=classify_phase_label(active_anchor),
        phase_line=active_anchor,
    )
    if not segments:
        _append_segment(
            segments,
            kind="active",
            start_offset_sec=0.0,
            stop_offset_sec=total_duration_sec,
            phase_label=classify_phase_label(lines[0]),
            phase_line=lines[0],
        )

    chapters: list[dict] = []
    last_chapter_offset: float | None = None
    last_label = ""
    for segment in segments:
        label = segment["phase_label"]
        offset = float(segment["start_offset_sec"])
        if last_chapter_offset is not None and abs(offset - last_chapter_offset) < CHAPTER_DEDUP_SEC and label == last_label:
            continue
        chapters.append(
            {
                "chapter_id": f"chapter-{len(chapters) + 1:03d}",
                "start_offset_sec": round(offset, 3),
                "label": label,
                "kind": segment["kind"],
                "phase_source": "trace",
                "is_idle": segment["kind"] == "idle",
            }
        )
        last_chapter_offset = offset
        last_label = label

    if not chapters or chapters[-1]["start_offset_sec"] != total_duration_sec:
        chapters.append(
            {
                "chapter_id": f"chapter-{len(chapters) + 1:03d}",
                "start_offset_sec": total_duration_sec,
                "label": "Run complete",
                "kind": "marker",
                "phase_source": "trace",
                "is_idle": False,
            }
        )

    return {
        "trace_event_count": len(lines),
        "trace_started_at_local": lines[0].stamp.isoformat(),
        "trace_stopped_at_local": lines[-1].stamp.isoformat(),
        "trace_duration_sec": total_duration_sec,
        "chapters": chapters,
        "segments": segments,
        "idle_segment_count": sum(1 for item in segments if item["kind"] == "idle"),
        "active_segment_count": sum(1 for item in segments if item["kind"] == "active"),
    }
