# Trace Analytics Build Plan

## Immediate Engineering Tasks (Codex)

1. **Upgrade run summarizer**  
   - Extend `qc-trace-summarize.ps1` to output normalized JSON records per run (machine, method, user, status, start/end UTC, duration, dialog/error counts, production/test flag derived from machine name and simulation markers).  
   - Write nightly outputs to a `summaries/` folder for downstream ingestion.

2. **Aggregate structured dataset**  
   - Build a script that pulls the JSON summaries from across the network share and consolidates them into a single CSV/SQLite table keyed by `run_id` (machine + timestamp).  
   - Include helper queries for common metrics (per-machine/day counts, abort rates, error frequency).

3. **Starter dashboards/exports**  
   - Produce reusable exports (CSV or PBIX data model) showing run volume, median duration, abort percentage, most common error patterns, simulation vs production mix.  
   - Document refresh steps so ops can update visuals after each data pull.

4. **Unknown pattern monitoring**  
   - Enhance `qc-trace-patterns.ps1` (or companion script) to compare the latest run against previous `qc-unknown-patterns.log` entries and surface newly observed patterns (e.g., via console summary or notification hook).  
   - Provide guidance on how to whitelist patterns into `expected-patterns.json`.

5. **Analytics runbook**  
   - Create documentation explaining the analysis pipeline: data sources, scripts, expected runtimes, how to add new patterns/metrics, and how to trigger a full refresh.

6. **Private GitHub backup**  
   - Initialize a private GitHub repository and push this toolkit (scripts, docs, TODOs) with clear setup instructions so the lab can collaborate outside this local workspace.

7. **MySQL enrichment pipeline**  
   - Implement a read-only connector to the lab MySQL database (instrument_status, instrument_ocupation, vibrant_test_tracking tables) and join relevant records into the trace summaries.  
   - Surface additional fields (current instrument status, plate IDs, assays) and expose them via the consolidated dataset for dashboard consumption.

## Follow-up Enhancements

Once the above is complete, plan the next data-layer steps (database ingestion, alerting hooks, richer visualizations) with stakeholders.
