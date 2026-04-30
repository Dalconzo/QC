# Central Replay Architecture

This document captures the first concrete design for the LAN-accessible replay
server that will sit above the current workstation-local camera stack.

The local camera flow is already good enough to produce durable artifacts:

- one MP4 per `HxRun.exe` session
- one `.run.json` manifest that pairs the video to the nearest Hamilton trace
- one replay catalog on each workstation for immediate local viewing

The next layer is a central service that ingests those workstation artifacts
into a shared SQL-backed catalog so engineers can browse runs over the LAN
without logging into each Hamilton PC.

The first implementation slice now exists locally: workstations can stage
completed runs into deterministic batch folders with copied artifacts, hashes,
and `run-upload.json` payloads. That staging step is intentionally offline so
we can verify the contract and dedup logic before introducing the LAN service.
The next slice now also exists: a filesystem-backed uploader can ingest those
staged runs into a shared root, assign `central_run_id` values, populate the
central SQLite catalog, and write acknowledgements back to the workstation.
The staging ledger also caches source-file hash results behind file stat
fingerprints so repeated auto-upload scans do not keep re-reading unchanged
video and trace files from the workstation disk.
The current storage pass also keeps local `run_id` values path-independent and
scrubs workstation-local paths from staged manifest copies so moved run folders
do not look like brand-new central artifacts.

## Goals

- Keep the current workstation-local replay path fast and usable.
- Add a central catalog that other users can browse over the LAN.
- Treat workstation upload as a second step after local capture succeeds.
- Preserve enough metadata to audit every uploaded file and every ingest run.
- Keep run traces and future multi-camera expansions representable without
  redesigning the schema again.

## Non-Goals For V1

- Live streaming from instrument PCs to the LAN server.
- Multi-camera synchronization in the first central schema.
- Real-time event overlays during active recording.
- Deleting workstation-local artifacts automatically before retention policy is
  defined and verified.

## Current Local Artifact Model

Today one completed local run produces:

- `video_path`
  - one MP4 recorded from one configured camera source
- `trace_path`
  - one Hamilton `.trc` file selected by closest last-write time to recorder
    shutdown
- `.run.json`
  - a pairing artifact with timing, source, stop reason, and selected trace

The local replay app also parses the trace file into timed events on demand.

## Design Constraint: Local `run_id` Is Not A Good Central Primary Key

The current local replay app now computes `run_id` from path-independent run
metadata, trace-derived structure, and timing. That is better for workstation
and staging stability, but it is still not a good central shared primary key
because:

- workstation clone roots can differ (`C:\QC`, `C:\camera-tools`, etc.)
- local identities can still collide in theory across sites or future recorder variants
- the central catalog still needs its own durable server-issued identity and audit trail

So the central catalog should store:

- `local_run_id`
  - the workstation-local replay identifier if present
- `central_run_id`
  - a server-issued stable identifier
- artifact hashes
  - used for deduplication and ingest verification

## Central System Overview

The central system has four layers:

1. Workstation capture
   - the current local daemon records video, pairs the trace, and writes
     `.run.json`
2. Workstation staging/upload
   - the current local stager packages completed runs into offline batch
     folders; the current prototype uploader ingests those staged artifacts
     into a filesystem-backed LAN root that matches the central contract
     service
3. Central ingest
   - the LAN service validates the payload, stores files, hashes them, and
     writes SQL catalog rows
4. Central replay/browse
   - users browse indexed runs, open replay pages, and view the reconstructed
     terminal against stored video and trace artifacts

## Proposed Artifact Contract

Each workstation upload should treat one completed run as one ingest unit.

Required metadata:

- workstation identity
  - hostname
  - machine alias if known
  - optional Hamilton instrument name
- camera profile identity
  - profile id
  - profile label
  - source description
- local run metadata
  - local run id
  - label
  - started/stopped local timestamps
  - duration
  - stop reason
  - process gate
- pairing metadata
  - Hamilton log directory used locally
  - log glob
  - selected trace filename
  - trace pairing delta seconds
- artifacts
  - video file
  - trace file
  - manifest file

Recommended additional metadata:

- artifact SHA-256 hashes
- artifact sizes in bytes
- workstation software version or git commit
- upload timestamp

## Proposed Central Storage Model

The central store should separate catalog metadata from large binary artifacts.

Catalog in SQL:

- workstations
- camera profiles
- runs
- artifacts
- ingest batches
- ingest items / ingest events

Binary artifacts on disk or network storage:

- video MP4 files
- trace `.trc` files
- original `.run.json`

The SQL catalog stores relative storage paths, hashes, sizes, and status. The
files themselves stay on a managed filesystem share instead of being embedded
directly into SQL blobs in V1.

That choice keeps the first central rollout simpler and avoids pushing large
video payloads into SQLite or a transactional database too early.

## Proposed Core Entities

### `workstations`

One row per deployed camera workstation.

Used to answer:

- which Hamilton PC produced this run
- which local install or hostname uploaded it

### `camera_profiles`

One row per workstation camera profile.

Used to answer:

- which camera recorded the run
- how we should label it in the UI

### `runs`

One row per completed replayable run.

Used to answer:

- what happened
- when it happened
- whether replay is ready
- which artifacts belong to it

### `artifacts`

One row per stored file for a run.

Used to answer:

- where the stored video/trace/manifest lives
- whether the upload is complete
- whether content is duplicated or missing

### `ingest_batches`

One row per workstation upload session.

Used to answer:

- what was uploaded together
- whether ingest succeeded
- what the server did with it

### `ingest_items`

One row per artifact observed during one ingest batch.

Used to answer:

- whether every expected file was received
- whether hashes matched
- which items failed validation

## Replay Status Model

The central catalog should use explicit status values instead of inferring them
on every page load.

Recommended run statuses:

- `pending_upload`
- `uploaded`
- `artifacts_missing`
- `ready`
- `quarantined`
- `archived`

Recommended ingest item statuses:

- `received`
- `hashed`
- `stored`
- `duplicate`
- `rejected`
- `missing`

## Artifact Types

The first schema should support these artifact types:

- `video_mp4`
- `trace_trc`
- `run_manifest_json`

Reserve room for later:

- `trace_events_jsonl`
  - pre-parsed terminal events for faster replay
- `thumbnail_jpg`
- `transcoded_video_mp4`
  - denser long-term compressed version

## Storage Layout

Use a deterministic relative path layout under the central artifact root:

`runs/<year>/<month>/<workstation>/<central_run_id>/`

Example:

`runs/2026/04/H7-CAM01/01J8.../`

Within that folder:

- `run.mp4`
- `trace.trc`
- `run.json`
- later optional derivatives such as `trace-events.jsonl` or thumbnails

This makes it easy to archive one run as one directory while keeping SQL rows
as the source of truth.

## Initial Upload Contract

The workstation uploader should submit:

1. one JSON metadata payload
2. the three required files

The central server should:

1. register or look up the workstation
2. register or look up the camera profile
3. create an ingest batch
4. store each artifact under the central artifact root
5. hash each stored file
6. create or update the `runs` row
7. create `artifacts` rows
8. record one `ingest_items` row per uploaded file
9. mark the run `ready` only when required artifacts are present

## Operator Workflows The Central API Must Support

- browse recent runs
- filter by workstation, date, readiness, and label
- open one run replay page
- inspect missing artifact problems
- refresh or rescan a workstation upload
- quarantine a bad run without deleting its audit trail

## Consequences For Local Workstation Artifacts

This design implies a few future changes to local output:

- local manifests should eventually include a workstation identifier
- local manifests should eventually include artifact hashes
- local replay should keep working even if upload to the LAN server is delayed
- workstation-local retention must not delete artifacts that have not been
  centrally verified yet

## Recommended Execution Order

1. Freeze this central schema and ingest contract.
2. Build a central schema bootstrap and local test fixture.
3. Implement the first uploader that sends completed local runs.
4. Implement the first central browse API against the SQL catalog.
5. Add retention/compression only after ingest verification is stable.

## Immediate Follow-On Tasks

- `QC-uy7`
  - central replay catalog schema and ingest contract
- `QC-vc7`
  - central replay server API and operator workflows
- `QC-4b6`
  - replace log upload workflow with replay artifact ingestion
- `QC-0ez`
  - define local retention/compression gates
- `QC-jio`
  - define central retention/compression gates
