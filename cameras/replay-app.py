#!/usr/bin/env python3
"""
replay-app.py

Local replay UI for Hamilton camera runs.

The recorder writes one `.run.json` manifest per captured run. This app scans
those manifests, serves the paired video, parses the Hamilton trace into timed
events, and exposes a lightweight browser UI that keeps the terminal panel in
sync with the current playback time in both directions.

The implementation intentionally uses only the Python standard library so the
camera workstation does not need extra web-framework dependencies.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, get_profile, load_effective_config, validate_config


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "replay_static"
TRACE_LINE_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})> ?(?P<body>.*)$")
CATALOG_FILENAME = ".replay_catalog.sqlite3"


@dataclass
class TraceEvent:
    """One Hamilton trace line paired with elapsed time from trace start."""

    index: int
    elapsed_sec: float
    stamp_local: str
    line: str


def find_ffmpeg(explicit: str | None = None) -> str | None:
    """Locate ffmpeg for live preview frame capture.

    The replay workstation should reuse the same ffmpeg discovery rules as the
    recorder so the live-preview path works on machines that rely on the repo's
    bundled ffmpeg.exe instead of a system-wide install.
    """
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.exists():
            return str(explicit_path)

    here = Path(__file__).parent
    for candidate in (
        here / "ffmpeg.exe",
        here / "dist" / "ffmpeg.exe",
        Path.cwd() / "cameras" / "ffmpeg.exe",
        Path.cwd() / "cameras" / "dist" / "ffmpeg.exe",
    ):
        if candidate.exists():
            return str(candidate)

    return shutil.which("ffmpeg")


def _normalize_dshow_name(source: str) -> str:
    """Convert user-friendly DirectShow syntax into ffmpeg input form."""
    value = source
    if value.lower().startswith("dshow:"):
        value = value.split(":", 1)[1]
    if value.lower().startswith("video="):
        key, raw_name = value.split("=", 1)
        raw_name = raw_name.strip().strip('"')
        return f"{key}={raw_name}"
    return value


def build_live_frame_command(
    ffmpeg_bin: str,
    source: str,
    *,
    framerate: int | None,
    video_size: str | None,
    jpeg_quality: int,
) -> list[str]:
    """Build a one-frame ffmpeg capture command for live preview."""
    src = (source or "").strip()
    if not src:
        raise ValueError("Camera profile source is empty.")

    is_rtsp = src.lower().startswith("rtsp")
    is_dshow = src.lower().startswith("dshow:") or src.lower().startswith("video=") or src.lower().startswith("audio=")

    if is_rtsp:
        input_args = ["-rtsp_transport", "tcp", "-i", src]
    elif is_dshow:
        input_args = ["-f", "dshow"]
        if framerate:
            input_args += ["-framerate", str(framerate)]
        if video_size:
            input_args += ["-video_size", str(video_size)]
        input_args += ["-i", _normalize_dshow_name(src)]
    else:
        input_args = ["-i", src]

    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        *input_args,
        "-frames:v",
        "1",
        "-q:v",
        str(jpeg_quality),
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]


def summarize_profile(profile: dict) -> dict:
    """Return lightweight metadata the live-view picker needs."""
    return {
        "id": profile.get("id") or "",
        "label": profile.get("label") or profile.get("id") or "Camera",
        "source": profile.get("source") or "",
        "framerate": profile.get("framerate"),
        "video_size": profile.get("video_size"),
    }


def list_live_profiles(config: dict) -> dict:
    """Expose configured camera profiles for the live-preview UI."""
    profiles = [summarize_profile(profile) for profile in config.get("profiles", [])]
    live_config = config.get("live", {})
    return {
        "items": profiles,
        "default_profile": str(live_config.get("default_profile") or ""),
        "refresh_ms": int(live_config.get("refresh_ms") or 1000),
    }


def capture_live_frame(config: dict, profile_id: str | None = None) -> tuple[bytes, dict]:
    """Capture one JPEG preview frame from a configured camera profile.

    The live view is intentionally stateless in v1: each browser refresh asks
    ffmpeg for one frame. That keeps the preview path easy to reason about and
    avoids introducing a long-lived stream manager before we know how these
    workstation cameras behave under real rollout conditions.
    """
    live_config = config.get("live", {})
    profile = get_profile(config, profile_id or live_config.get("default_profile") or None)
    ffmpeg_bin = find_ffmpeg(profile.get("ffmpeg_path") or config.get("recorder", {}).get("ffmpeg_path") or "")
    if not ffmpeg_bin:
        raise FileNotFoundError("ffmpeg was not found for live preview capture.")

    cmd = build_live_frame_command(
        ffmpeg_bin,
        str(profile.get("source") or ""),
        framerate=profile.get("framerate"),
        video_size=profile.get("video_size"),
        jpeg_quality=int(live_config.get("jpeg_quality") or 4),
    )

    timeout_sec = int(live_config.get("frame_timeout_sec") or 8)
    completed = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout_sec)
    if completed.returncode != 0:
        error_text = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        if not error_text:
            error_text = f"ffmpeg exited with code {completed.returncode}"
        raise RuntimeError(error_text)
    if not completed.stdout:
        raise RuntimeError("ffmpeg returned no image data for live preview.")

    return completed.stdout, summarize_profile(profile)


def parse_trace_events(trace_path: Path) -> list[TraceEvent]:
    """Convert the raw `.trc` file into replayable timed events."""
    events: list[TraceEvent] = []
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
            events.append(
                TraceEvent(
                    index=len(events),
                    elapsed_sec=elapsed_sec,
                    stamp_local=stamp.isoformat(sep=" "),
                    line=line,
                )
            )
    return events


def load_run_manifest(manifest_path: Path) -> dict:
    """Read one run manifest and normalize common fields."""
    with manifest_path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)

    payload["manifest_path"] = str(manifest_path.resolve())
    payload["video_path"] = str(Path(payload.get("video_path", "")).resolve()) if payload.get("video_path") else ""
    payload["trace_path"] = str(Path(payload.get("trace_path", "")).resolve()) if payload.get("trace_path") else ""
    payload["run_id"] = compute_run_id(manifest_path, payload)
    return payload


def compute_run_id(manifest_path: Path, payload: dict) -> str:
    """Build a stable run identifier from the manifest identity and timing."""
    identity = {
        "manifest_path": str(manifest_path.resolve()),
        "video_path": str(Path(payload.get("video_path", "")).resolve()) if payload.get("video_path") else "",
        "trace_path": str(Path(payload.get("trace_path", "")).resolve()) if payload.get("trace_path") else "",
        "started_at_local": payload.get("started_at_local") or "",
        "stopped_at_local": payload.get("stopped_at_local") or "",
        "label": payload.get("label") or "",
    }
    return hashlib.sha1(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def get_catalog_path(runs_root: Path) -> Path:
    """Store the local replay catalog beside the run artifacts it indexes."""
    return runs_root / CATALOG_FILENAME


def get_db_connection(catalog_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with row access by column name."""
    conn = sqlite3.connect(catalog_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_catalog_db(conn: sqlite3.Connection) -> None:
    """Create the local replay catalog schema if it does not exist yet."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            manifest_path TEXT NOT NULL UNIQUE,
            manifest_mtime_ns INTEGER NOT NULL,
            label TEXT NOT NULL,
            source TEXT NOT NULL,
            video_path TEXT NOT NULL,
            video_filename TEXT NOT NULL,
            trace_path TEXT NOT NULL,
            trace_filename TEXT NOT NULL,
            started_at_local TEXT,
            stopped_at_local TEXT,
            duration_sec REAL,
            stop_reason TEXT,
            process_gate TEXT,
            trace_mtime_delta_sec REAL,
            has_video INTEGER NOT NULL,
            has_trace INTEGER NOT NULL,
            replay_status TEXT NOT NULL,
            cataloged_at_utc TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at_local DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(replay_status, started_at_local DESC);
        """
    )
    conn.commit()


def iter_manifest_paths(runs_root: Path) -> list[Path]:
    """Return manifest files in newest-first order for catalog refresh."""
    return sorted(runs_root.rglob("*.run.json"), key=lambda path: path.stat().st_mtime, reverse=True)


def determine_replay_status(payload: dict) -> str:
    """Flag whether the catalog entry is immediately replayable."""
    has_video = bool(payload.get("video_path") and Path(payload["video_path"]).exists())
    has_trace = bool(payload.get("trace_path") and Path(payload["trace_path"]).exists())
    if has_video and has_trace:
        return "ready"
    if has_video:
        return "missing_trace"
    if has_trace:
        return "missing_video"
    return "missing_video_and_trace"


def refresh_catalog(runs_root: Path) -> dict:
    """Rebuild the local replay catalog from the durable run manifests.

    The replay app still uses `.run.json` as the source artifact written by the
    recorder, but the picker should browse a stable local index rather than
    walking the directory tree on every request. Refreshing this catalog keeps
    the UI responsive and gives us a schema we can later lift into a central
    SQL-backed replay service.
    """
    runs_root.mkdir(parents=True, exist_ok=True)
    catalog_path = get_catalog_path(runs_root)
    manifests = iter_manifest_paths(runs_root)

    with closing(get_db_connection(catalog_path)) as conn:
        init_catalog_db(conn)
        seen_manifest_paths: set[str] = set()
        cataloged_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

        for manifest_path in manifests:
            payload = load_run_manifest(manifest_path)
            payload["has_video"] = bool(payload.get("video_path") and Path(payload["video_path"]).exists())
            payload["has_trace"] = bool(payload.get("trace_path") and Path(payload["trace_path"]).exists())
            payload["replay_status"] = determine_replay_status(payload)
            seen_manifest_paths.add(payload["manifest_path"])

            conn.execute(
                """
                INSERT INTO runs (
                    run_id,
                    manifest_path,
                    manifest_mtime_ns,
                    label,
                    source,
                    video_path,
                    video_filename,
                    trace_path,
                    trace_filename,
                    started_at_local,
                    stopped_at_local,
                    duration_sec,
                    stop_reason,
                    process_gate,
                    trace_mtime_delta_sec,
                    has_video,
                    has_trace,
                    replay_status,
                    cataloged_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    manifest_path = excluded.manifest_path,
                    manifest_mtime_ns = excluded.manifest_mtime_ns,
                    label = excluded.label,
                    source = excluded.source,
                    video_path = excluded.video_path,
                    video_filename = excluded.video_filename,
                    trace_path = excluded.trace_path,
                    trace_filename = excluded.trace_filename,
                    started_at_local = excluded.started_at_local,
                    stopped_at_local = excluded.stopped_at_local,
                    duration_sec = excluded.duration_sec,
                    stop_reason = excluded.stop_reason,
                    process_gate = excluded.process_gate,
                    trace_mtime_delta_sec = excluded.trace_mtime_delta_sec,
                    has_video = excluded.has_video,
                    has_trace = excluded.has_trace,
                    replay_status = excluded.replay_status,
                    cataloged_at_utc = excluded.cataloged_at_utc
                """,
                (
                    payload["run_id"],
                    payload["manifest_path"],
                    manifest_path.stat().st_mtime_ns,
                    payload.get("label") or "run",
                    payload.get("source") or "",
                    payload.get("video_path") or "",
                    Path(payload["video_path"]).name if payload.get("video_path") else "",
                    payload.get("trace_path") or "",
                    Path(payload["trace_path"]).name if payload.get("trace_path") else "",
                    payload.get("started_at_local"),
                    payload.get("stopped_at_local"),
                    payload.get("duration_sec"),
                    payload.get("stop_reason"),
                    payload.get("process_gate"),
                    payload.get("trace_mtime_delta_sec"),
                    int(payload["has_video"]),
                    int(payload["has_trace"]),
                    payload["replay_status"],
                    cataloged_at,
                ),
            )

        if seen_manifest_paths:
            placeholders = ",".join("?" for _ in seen_manifest_paths)
            conn.execute(
                f"DELETE FROM runs WHERE manifest_path NOT IN ({placeholders})",
                tuple(seen_manifest_paths),
            )
        else:
            conn.execute("DELETE FROM runs")

        conn.commit()
        row = conn.execute("SELECT COUNT(*) AS run_count FROM runs").fetchone()

    return {
        "catalog_path": str(catalog_path.resolve()),
        "manifest_count": len(manifests),
        "run_count": int(row["run_count"]) if row else 0,
        "refreshed_at_utc": cataloged_at,
    }


def summarize_catalog_row(row: sqlite3.Row) -> dict:
    """Return lightweight metadata for the run picker."""
    return {
        "run_id": row["run_id"],
        "label": row["label"],
        "started_at_local": row["started_at_local"],
        "stopped_at_local": row["stopped_at_local"],
        "duration_sec": row["duration_sec"],
        "stop_reason": row["stop_reason"],
        "trace_filename": row["trace_filename"],
        "video_filename": row["video_filename"],
        "has_trace": bool(row["has_trace"]),
        "has_video": bool(row["has_video"]),
        "replay_status": row["replay_status"],
        "trace_mtime_delta_sec": row["trace_mtime_delta_sec"],
        "process_gate": row["process_gate"],
    }


def list_catalog_runs(runs_root: Path) -> list[dict]:
    """Read the run picker from the local catalog instead of the filesystem."""
    catalog_path = get_catalog_path(runs_root)
    if not catalog_path.exists():
        refresh_catalog(runs_root)
    with closing(get_db_connection(catalog_path)) as conn:
        init_catalog_db(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM runs
            ORDER BY COALESCE(started_at_local, '') DESC, manifest_mtime_ns DESC
            """
        ).fetchall()
    return [summarize_catalog_row(row) for row in rows]


def get_latest_ready_run(runs_root: Path) -> dict | None:
    """Return the newest catalog entry that has both video and trace files.

    The local workstation launcher uses this to open engineers directly into
    the freshest replayable run instead of making them browse the picker after
    every assay.
    """
    catalog_path = get_catalog_path(runs_root)
    if not catalog_path.exists():
        refresh_catalog(runs_root)
    with closing(get_db_connection(catalog_path)) as conn:
        init_catalog_db(conn)
        row = conn.execute(
            """
            SELECT *
            FROM runs
            WHERE replay_status = 'ready'
            ORDER BY COALESCE(started_at_local, '') DESC, manifest_mtime_ns DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return summarize_catalog_row(row)


def get_catalog_run(runs_root: Path, run_id: str) -> dict:
    """Resolve one run from the local catalog."""
    catalog_path = get_catalog_path(runs_root)
    if not catalog_path.exists():
        refresh_catalog(runs_root)
    with closing(get_db_connection(catalog_path)) as conn:
        init_catalog_db(conn)
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return dict(row)


def summarize_run(run: dict) -> dict:
    """Return lightweight metadata for the run picker."""
    trace_path = Path(run["trace_path"]) if run.get("trace_path") else None
    video_path = Path(run["video_path"]) if run.get("video_path") else None
    return {
        "run_id": run["run_id"],
        "label": run.get("label") or "run",
        "started_at_local": run.get("started_at_local"),
        "stopped_at_local": run.get("stopped_at_local"),
        "duration_sec": run.get("duration_sec"),
        "stop_reason": run.get("stop_reason"),
        "trace_filename": trace_path.name if trace_path else "",
        "video_filename": video_path.name if video_path else "",
        "has_trace": bool(trace_path and trace_path.exists()),
        "has_video": bool(video_path and video_path.exists()),
        "replay_status": determine_replay_status(run),
        "trace_mtime_delta_sec": run.get("trace_mtime_delta_sec"),
        "process_gate": run.get("process_gate"),
    }


def get_run_by_id(runs_root: Path, run_id: str) -> dict:
    """Resolve one run through the catalog, then re-load its manifest."""
    row = get_catalog_run(runs_root, run_id)
    return load_run_manifest(Path(row["manifest_path"]))


def get_run_detail(runs_root: Path, run_id: str) -> dict:
    """Return the full API payload for one run."""
    run = get_run_by_id(runs_root, run_id)
    trace_path = Path(run["trace_path"]) if run.get("trace_path") else None
    events = parse_trace_events(trace_path) if trace_path and trace_path.exists() else []
    return {
        "run": summarize_run(run),
        "manifest": {
            "manifest_path": run.get("manifest_path"),
            "video_path": run.get("video_path"),
            "trace_path": run.get("trace_path"),
            "trace_mtime_delta_sec": run.get("trace_mtime_delta_sec"),
            "process_gate": run.get("process_gate"),
            "source": run.get("source"),
        },
        "events": [asdict(event) for event in events],
    }


def make_handler(runs_root: Path, config: dict):
    """Create a request handler bound to one manifest root."""

    class ReplayHandler(BaseHTTPRequestHandler):
        server_version = "HamiltonReplay/1.0"

        def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, body: bytes, *, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, *, content_type: str | None = None) -> None:
            mime = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            file_size = path.stat().st_size
            range_header = self.headers.get("Range")

            if range_header:
                start, end = self._parse_range_header(range_header, file_size)
                length = end - start + 1
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", mime)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                with path.open("rb") as handle:
                    handle.seek(start)
                    self.wfile.write(handle.read(length))
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with path.open("rb") as handle:
                self.wfile.write(handle.read())

        def _parse_range_header(self, header_value: str, file_size: int) -> tuple[int, int]:
            """Parse a simple single-range header for MP4 seeking.

            Browsers typically request one byte range at a time while the user
            scrubs through the timeline. Supporting that pattern makes the
            built-in HTML5 video controls behave like a local file instead of
            snapping back to the current position.
            """
            match = re.match(r"bytes=(\d*)-(\d*)$", header_value.strip())
            if not match:
                raise ValueError("Unsupported Range header")

            start_text, end_text = match.groups()
            if not start_text and not end_text:
                raise ValueError("Empty Range header")

            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else file_size - 1
            else:
                suffix_length = int(end_text)
                suffix_length = min(suffix_length, file_size)
                start = file_size - suffix_length
                end = file_size - 1

            if start < 0 or end < start or end >= file_size:
                raise ValueError("Invalid Range header")
            return start, end

        def _send_text(self, text: str, status: HTTPStatus) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            parsed = urlparse(self.path)
            route = unquote(parsed.path)
            params = parse_qs(parsed.query)

            if route == "/":
                return self._send_file(STATIC_DIR / "index.html", content_type="text/html; charset=utf-8")

            if route.startswith("/static/"):
                asset_path = (STATIC_DIR / route.removeprefix("/static/")).resolve()
                if STATIC_DIR not in asset_path.parents and asset_path != STATIC_DIR:
                    return self._send_text("Invalid static path", HTTPStatus.BAD_REQUEST)
                if not asset_path.exists() or not asset_path.is_file():
                    return self._send_text("Static asset not found", HTTPStatus.NOT_FOUND)
                return self._send_file(asset_path)

            if route == "/api/runs":
                runs = list_catalog_runs(runs_root)
                return self._send_json({"items": runs, "catalog_path": str(get_catalog_path(runs_root).resolve())})

            if route == "/api/runs/latest":
                latest = get_latest_ready_run(runs_root)
                return self._send_json(
                    {
                        "item": latest,
                        "catalog_path": str(get_catalog_path(runs_root).resolve()),
                    }
                )

            if route == "/api/catalog/refresh":
                return self._send_json(refresh_catalog(runs_root))

            if route == "/api/live/profiles":
                return self._send_json(list_live_profiles(config))

            if route == "/api/live/frame.jpg":
                profile_id = (params.get("profile") or [""])[0].strip() or None
                try:
                    image_bytes, profile = capture_live_frame(config, profile_id)
                except KeyError:
                    return self._send_text("Camera profile not found", HTTPStatus.NOT_FOUND)
                except FileNotFoundError as exc:
                    return self._send_text(str(exc), HTTPStatus.SERVICE_UNAVAILABLE)
                except subprocess.TimeoutExpired:
                    return self._send_text("Timed out while capturing a live preview frame.", HTTPStatus.GATEWAY_TIMEOUT)
                except RuntimeError as exc:
                    return self._send_text(f"Failed to capture live preview: {exc}", HTTPStatus.BAD_GATEWAY)
                response_profile = profile.get("id") or ""
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(image_bytes)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Camera-Profile", response_profile)
                self.end_headers()
                self.wfile.write(image_bytes)
                return

            if route.startswith("/api/runs/"):
                parts = [part for part in route.split("/") if part]
                if len(parts) < 3:
                    return self._send_text("Invalid run route", HTTPStatus.BAD_REQUEST)
                run_id = parts[2]
                try:
                    run = get_run_by_id(runs_root, run_id)
                except KeyError:
                    return self._send_text("Run not found", HTTPStatus.NOT_FOUND)

                if len(parts) == 3:
                    return self._send_json(get_run_detail(runs_root, run_id))

                if len(parts) == 4 and parts[3] == "video":
                    video_path = Path(run["video_path"]) if run.get("video_path") else None
                    if not video_path or not video_path.exists():
                        return self._send_text("Run video is missing", HTTPStatus.NOT_FOUND)
                    try:
                        return self._send_file(video_path)
                    except ValueError:
                        return self._send_text("Invalid Range header", HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)

            return self._send_text("Not found", HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args) -> None:  # noqa: A003 - stdlib signature
            return

    return ReplayHandler


def main() -> int:
    parser = argparse.ArgumentParser(description="Local replay UI for Hamilton run videos and traces")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    parser.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Path to the optional workstation-local override JSON")
    parser.add_argument(
        "--runs-root",
        default="",
        help="Directory to scan for *.run.json manifests",
    )
    parser.add_argument("--host", default="", help="Bind host for the local replay server")
    parser.add_argument("--port", type=int, default=None, help="Bind port for the local replay server")
    args = parser.parse_args()

    config = load_effective_config(
        config_path=Path(args.config),
        local_override_path=Path(args.local_config),
    )
    validation = validate_config(config, require_hamilton_log_dir=False)
    if validation["errors"]:
        for item in validation["errors"]:
            print(item)
        return 1

    runs_root = Path(args.runs_root or config["storage"]["runs_root"])
    host = args.host or config["replay"]["host"]
    port = args.port if args.port is not None else int(config["replay"]["port"])
    refresh_result = refresh_catalog(runs_root)
    handler = make_handler(runs_root, config)
    with ThreadingHTTPServer((host, port), handler) as server:
        print(f"Serving Hamilton replay UI on http://{host}:{port}")
        print(f"Scanning manifests under: {runs_root}")
        print(f"Catalog path: {refresh_result['catalog_path']}")
        print(f"Cataloged runs: {refresh_result['run_count']}")
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
