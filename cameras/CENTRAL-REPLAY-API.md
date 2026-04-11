# Central Replay API And Workflows

This document defines the first API shape and operator workflows for the
planned LAN replay server.

It is intentionally aligned to:

- [`CENTRAL-REPLAY-ARCHITECTURE.md`](/C:/QC/cameras/CENTRAL-REPLAY-ARCHITECTURE.md)
- [`sql/central-replay-schema.sql`](/C:/QC/cameras/sql/central-replay-schema.sql)

## V1 Service Responsibilities

The central service should do three things well first:

1. Accept completed workstation uploads.
2. Catalog runs and artifacts in SQL.
3. Serve a browseable replay UI/API over the LAN.

## API Areas

### 1. Ingest API

Used by Hamilton workstations or a future uploader agent.

The current prototype implements this contract through a filesystem-backed
uploader instead of HTTP. The endpoint shape below remains the target service
contract once the LAN host is running as an application server.

Required operations:

- create ingest batch
- upload one completed run
- finalize ingest batch
- query ingest batch status

### 2. Catalog API

Used by the LAN replay web UI and troubleshooting tools.

Required operations:

- list runs
- fetch one run
- list artifacts for one run
- list workstations
- list camera profiles

### 3. Admin API

Used by operators and engineering staff.

Required operations:

- refresh/rescan a workstation
- quarantine a run
- mark a run archived
- inspect ingest failures

## Proposed V1 Endpoints

### Ingest

`POST /api/ingest/batches`

- create one ingest batch
- returns `ingest_batch_id`

`POST /api/ingest/runs`

- upload one completed run payload
- multipart or staged-file workflow
- request contains:
  - metadata JSON
  - `video_mp4`
  - `trace_trc`
  - `run_manifest_json`

`POST /api/ingest/batches/{batch_id}/complete`

- mark the batch complete
- server validates counts and statuses

`GET /api/ingest/batches/{batch_id}`

- inspect ingest status and item-level failures

### Catalog

`GET /api/runs`

Query parameters:

- `workstation_id`
- `camera_profile_id`
- `started_after`
- `started_before`
- `replay_status`
- `limit`
- `cursor`

`GET /api/runs/{central_run_id}`

- full run metadata
- artifact list
- ingest summary

`GET /api/runs/{central_run_id}/artifacts`

- artifact metadata only

`GET /api/workstations`

- deployed workstation list

`GET /api/camera-profiles`

- active camera profiles across workstations

### Replay

`GET /api/runs/{central_run_id}/trace-events`

- returns replay-ready timed trace events
- may be generated on demand from stored `.trc` at first
- may later come from a cached derivative artifact

`GET /media/{storage_relpath}`

- byte-range capable media endpoint for MP4 replay

### Admin

`POST /api/admin/runs/{central_run_id}/quarantine`

- quarantine bad or suspicious runs without deleting history

`POST /api/admin/runs/{central_run_id}/archive`

- mark run archived after retention/compression actions

`GET /api/admin/ingest-failures`

- recent ingest problems for troubleshooting

## Workstation Upload Workflow

1. Local daemon records the run and writes `.run.json`.
2. Local uploader discovers completed runs that are not centrally verified yet.
3. Uploader creates an ingest batch.
4. Uploader sends one completed run payload at a time.
5. Server stores files, hashes them, and updates SQL rows.
6. Server returns the new `central_run_id` and artifact statuses.
7. Uploader marks the local run as centrally acknowledged.

Important rule:

- local artifacts must remain replayable on the workstation even if central
  upload is delayed or unavailable

## Operator Browse Workflow

1. Open the LAN replay site.
2. Browse recent runs.
3. Filter by workstation/date/status.
4. Open one run.
5. View video on top and synchronized terminal below, using the same replay
   interaction model as the local workstation app.

## Operator Troubleshooting Workflow

1. Check ingest failures.
2. Open the affected batch or run.
3. Inspect missing artifacts, bad hashes, or path mismatches.
4. Quarantine the bad run if needed.
5. Re-run upload for the workstation once the source issue is fixed.

## API Response Principles

- Use explicit status fields, not inferred UI-only states.
- Include stable IDs in every response.
- Preserve local identifiers for traceability, but do not treat them as global
  primary keys.
- Return enough artifact metadata for operators to diagnose missing files
  without logging into the workstation.

## V1 Authentication Assumption

For the first LAN prototype, assume the service runs on a trusted internal
network and start with minimal authentication. Do not bake unauthenticated
internet exposure assumptions into the design.

If access control becomes necessary, add it at the HTTP layer without changing
the core run/artifact schema.

## Immediate Implementation Consequences

- The media endpoint must support byte ranges, because browser video seeking
  depends on it.
- The run detail endpoint should expose trace events as a separate payload so
  replay clients do not need direct filesystem access to `.trc` files.
- The ingest API must capture hashes and file sizes at ingest time, not later.

## Deferred Items

- live cross-machine viewing
- multi-camera run grouping
- operator annotations
- derived automatic chapter markers from trace phases
- stronger archival transcodes and cold-storage restore flows
