# Trace Monitor Toolkit

Organizes Hamilton trace ingestion, summarization, enrichment, and dashboards.

Key entry points moved into a clean layout for easier navigation:

- scripts/ps
  - PowerShell utilities: daily copy, backfill, summarizer, pattern scan, aggregation, DB wrapper, edge watcher.
- scripts/py
  - Python utilities: enrichment + DB helpers (read‑only), mapping inference, discovery.
- config
  - Config files: expected patterns, machine aliases, MySQL DSN (read‑only).
- docs
  - Project docs and TODOs.
- viz_hamilton_usage
  - Minimal Flask dashboard for live Hamilton usage (read‑only).
- data/samples
  - Sample .trc files for testing (non‑production).
- logs (git‑ignored)
  - Local logs including unknown‑pattern log.
- outbox, summaries (git‑ignored)
  - Staging directories for generated artifacts.

Quick commands

- Daily copy: `powershell -NoProfile -File C:\\QC\\scripts\\ps\\qc-hamilton-dailycopy.ps1`
- Backfill: `powershell -NoProfile -File C:\\QC\\scripts\\ps\\qc-hamilton-backfill.ps1`
- Summaries: `scripts/ps/qc-trace-summarize.ps1 -SourceRoot Z:\\Logs -Recurse -AsJson -OutDir .\\summaries -ByLocalDate`
- Aggregate: `scripts/ps/qc-aggregate-summaries.ps1 -SummariesDir .\\summaries -OutCsv .\\outbox\\run-summaries.csv -WriteHelperMetrics`
- Enrich (RO): `scripts/ps/qc-mysql-enrich.ps1 -InputCsv .\\outbox\\run-summaries.csv -DsnFile .\\config\\mysql_labsite.dsn -Database operation_data -TestsDatabase lab_scheduler`
- Patterns: `scripts/ps/qc-trace-patterns.ps1 -Root Z:\\Logs -Recurse -ExpectedPatternsPath .\\config\\expected-patterns.json -UnknownLogPath .\\logs\\qc-unknown-patterns.log -DeltaOnly`

Beads (work tracking)

- Initialize: `bd init`
- Ready work: `bd ready --json`
- Create task: `bd create "<title>" -t task -p 1 --json`
- Update: `bd update <id> --status in_progress --actor Codex --json`
- Close: `bd close <id> --reason "Implemented" --actor Codex --json`

