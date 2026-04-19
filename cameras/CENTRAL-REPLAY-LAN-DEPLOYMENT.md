# Central Replay LAN Deployment

This document captures the first practical LAN deployment for the current
filesystem-backed central replay transport.

## Current Host

- host machine: `DESKTOP-JL79UJV`
- host LAN IPv4: `192.168.70.121`
- central replay HTTP URL:
  - `http://DESKTOP-JL79UJV:5080/`
  - `http://192.168.70.121:5080/`
- central upload root on host:
  - `C:\QC\cameras\central_replay_root`
- central server config on host:
  - `C:\QC\config\central-replay-server.json`
  - `C:\QC\config\central-replay-server.local.json`

## Current Topology

The current transport is not HTTP ingest yet. The target workstation writes
into a shared filesystem path hosted by this machine, and the LAN server reads
that same local root.

Flow:

1. Target workstation records one local run.
2. Target workstation stages the `.run.json`, MP4, and `.trc` into its local
   staging root.
3. Target workstation uploads the staged bundle into a UNC path hosted by this
   machine.
4. The upload writes:
   - managed files under `storage\...`
   - `.central_replay_catalog.sqlite3`
   - local `run-ack.json` acknowledgements back on the workstation
5. Engineers browse the central replay site over HTTP from this machine.

## Host Startup

Start the LAN replay server on this machine with:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-central-replay-server.ps1 `
  -Background `
  -OpenBrowser
```

Inspect the resolved host config with:

```powershell
python C:\QC\cameras\central-replay-server.py `
  --server-config C:\QC\config\central-replay-server.json `
  --server-local-config C:\QC\config\central-replay-server.local.json `
  --print-config --json
```

Host-local health check:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5080/api/healthz
```

## Required One-Time Elevated Host Setup

These two commands must be run in an elevated PowerShell window on this host.
They were not completed from the current shell because Windows returned
`Access is denied`.

Create the upload share:

```powershell
New-SmbShare `
  -Name QCReplayUpload `
  -Path C:\QC\cameras\central_replay_root `
  -Description "QC central replay upload root" `
  -ChangeAccess Everyone
```

Open the HTTP port:

```powershell
New-NetFirewallRule `
  -DisplayName "QC Central Replay HTTP 5080" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 5080
```

If you want to tighten access later, replace `Everyone` with the specific user
or group that will run the uploader from the target workstation.

## Target Workstation Pipeline

The target workstation should keep its local staging root on the workstation,
but point `central_ingest.upload_root` at the UNC share on this machine.

Recommended target setting:

```json
{
  "central_ingest": {
    "staging_root": "C:\\QC\\cameras\\central_staging",
    "upload_root": "\\\\DESKTOP-JL79UJV\\QCReplayUpload",
    "transport": "filesystem",
    "auto_upload_on_run_complete": true,
    "status_server_url": "http://192.168.70.121:5080"
  }
}
```

Equivalent LAN path by IP:

```text
\\192.168.70.121\QCReplayUpload
```

Hostname is preferred so the path stays stable if DHCP changes the IP.

## Target Workstation Run Path

On the target workstation, the current run path should be:

1. record locally with the camera daemon / recorder
2. verify one replayable `.run.json`
3. run:

```powershell
powershell -NoProfile -File C:\QC\cameras\stage-central-replay.ps1 -AsJson
```

4. then run:

```powershell
powershell -NoProfile -File C:\QC\cameras\upload-central-replay.ps1 -AsJson
```

That uploader should be pointed at:

- `\\DESKTOP-JL79UJV\QCReplayUpload`

Expected result:

- target workstation receives `run-ack.json`
- host catalog gains one run row
- host `storage\...` gains the stored MP4, `.trc`, and manifest
- browse UI shows the new run

## Practical Smoke Test

Use this order once the elevated host steps are finished:

1. On this host:
   - start the central replay server
   - verify `http://127.0.0.1:5080/api/healthz`
2. On the target workstation:
   - confirm `Test-Path \\DESKTOP-JL79UJV\QCReplayUpload`
   - stage one completed run
   - upload one staged run
3. On this host:
   - confirm a new folder appears under `C:\QC\cameras\central_replay_root\storage`
   - confirm `.central_replay_catalog.sqlite3` row counts increase
4. On a second engineer machine:
   - open `http://DESKTOP-JL79UJV:5080/`
   - verify the run list, trace view, and MP4 playback

## Next Improvement After This Bring-Up

The current data pipeline is good enough for the first trusted-LAN deployment,
but it still depends on SMB write access from the target workstation. The next
cleaner cutover is the planned HTTP ingest service, which would let the target
workstation upload over HTTP while this host alone owns the filesystem layout.

## Immediate Status Pushes

With `central_ingest.status_server_url` configured, the workstation daemon also
posts:

- `POST /api/workstations/heartbeat`
- `POST /api/runs/status`

That lets the LAN UI show workstation presence plus run states like
`pending_upload`, `uploading`, `available`, and `failed` before the central
artifact upload is complete.
