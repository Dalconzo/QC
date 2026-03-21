# Analytics Runbook

Scope
- Summarize Hamilton traces, aggregate to a consolidated dataset, optionally enrich from MySQL (read-only), and feed dashboards.

Daily cadence
- 23:55 (or 00:05 with `-DaysBack 1`): mirror logs to `Z:\Logs` using daily copy.
- 00:15: summarize traces to JSONL by run date into `.\summaries`.
- 00:25: aggregate to CSV at `.\outbox\run-summaries.csv` and helper metrics under `.\outbox\metrics`.
- 00:30: optionally enrich CSV with read-only MySQL data into `.\outbox\run-summaries-enriched.csv`.
- Ad hoc: run pattern scan and review new unknown patterns.

Commands
- Daily copy: `powershell -NoProfile -File C:\QC\scripts\ps\qc-hamilton-dailycopy.ps1`
- Backfill: `powershell -NoProfile -File C:\QC\scripts\ps\qc-hamilton-backfill.ps1`
- Summaries: `scripts/ps/qc-trace-summarize.ps1 -SourceRoot Z:\Logs -Recurse -AsJson -OutDir .\summaries -ByLocalDate`
- Summarizer regression check: `powershell -NoProfile -File C:\QC\scripts\ps\qc-test-trace-summarize.ps1`
- Aggregate: `scripts/ps/qc-aggregate-summaries.ps1 -SummariesDir .\summaries -OutCsv .\outbox\run-summaries.csv -WriteHelperMetrics`
- Aggregation regression check: `powershell -NoProfile -File C:\QC\scripts\ps\qc-test-aggregate-summaries.ps1`
- Enrich (RO): `scripts/ps/qc-mysql-enrich.ps1 -InputCsv .\outbox\run-summaries.csv -DsnFile .\config\mysql_labsite.dsn -Database operation_data -TestsDatabase lab_scheduler`
- Enrich (no DB): `scripts/ps/qc-mysql-enrich.ps1 -InputCsv .\outbox\run-summaries.csv -OutCsv .\outbox\run-summaries-enriched.csv -SkipDb`
- Full pipeline regression check: `powershell -NoProfile -File C:\QC\scripts\ps\qc-test-analytics-pipeline.ps1`
- Enrichment regression check: `powershell -NoProfile -File C:\QC\scripts\ps\qc-test-mysql-enrich.ps1`
- Enrichment live smoke check: `powershell -NoProfile -File C:\QC\scripts\ps\qc-test-mysql-enrich-live.ps1`
- Build network verification fixture: `powershell -NoProfile -File C:\QC\scripts\ps\qc-build-network-fixture.ps1 -SourceRoot \\192.168.10.99\home\Logs -Force`
- Network fixture pipeline check: `powershell -NoProfile -File C:\QC\scripts\ps\qc-test-network-fixture.ps1`
- Inventory HxUsbComm logs: `powershell -NoProfile -File C:\QC\scripts\ps\qc-inventory-usbcomm-traces.ps1 -SourceRoot \\192.168.10.99\home\Logs -Recurse`
- Patterns: `scripts/ps/qc-trace-patterns.ps1 -Root Z:\Logs -Recurse -ExpectedPatternsPath .\config\expected-patterns.json -UnknownLogPath .\logs\qc-unknown-patterns.log -DeltaOnly`

Data sources
- Traces: `Z:\Logs\<Machine>\YYYY-MM-DD\*.trc`
- MySQL (RO, optional): `192.168.60.4:3307` user `labsite`.

Conventions
- Test machines: `H14`, `H13`, `H7`.
- Simulation detected via `SetSimulation`.
- Do not perform DB writes; connector enforces read-only access.

Operating stance
- Treat `.trc` files as the system of record for historical analytics.
- Keep method traces and `HxUsbComm*.trc` logs in separate storage lanes; usbcomm logs are useful firmware/command evidence, but they are not run-summary records.
- Treat MySQL enrichment as best-effort context only; do not make critical analytics depend on it.
- If the database is degraded, continue the trace pipeline and defer only the optional enrichment/dashboard layer.

Troubleshooting
- No JSONL found: confirm summaries step ran and paths are correct.
- Unexpected machine IDs in sample validation: run the summarizer regression check to confirm path-based and serial-based machine resolution still behaves as expected.
- Unexpected aggregate output: run the aggregation regression check to confirm row preservation, CSV schema, and helper metrics are still aligned.
- Empty enrichment: verify `config/mysql_labsite.dsn` and network reachability, or run with `-SkipDb` to intentionally produce blank enrichment columns without failing the pipeline.
- Pattern deltas: check `logs/qc-unknown-patterns.log`; whitelist updates in `config/expected-patterns.json`.
