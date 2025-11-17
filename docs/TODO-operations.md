# Operations & Data Capture To-Do (Lab Team)

This list tracks actions that require lab-side coordination or hardware/process changes outside the repo.

## Data Collection & Method Updates

- **Collect representative traces**  
  Gather additional `.trc` files from the uncertified/test machines (H14, H13, H7) and from methods that use specialized peripherals. Drop them into the repo (or referenced share) for parser tuning.

- **Embed run context markers**  
  Decide whether to add explicit `mode=test/simulation/production`, plate IDs, reagent lots, or other run context into the method scripts. Coordinate Hamilton method edits if approved.

- **Operator annotation workflow**  
  Establish a lightweight process (form/log) for technicians to capture notable events during runs; ensure entries can reference trace IDs or timestamps.

## Infrastructure & Tooling

- **Dashboard environment**  
  Pick the platform (Power BI workspace, Grafana, etc.), provision access, and ensure a stable connection to `Z:\Logs` or the consolidated dataset.

- **Telemetry extras**  
  Evaluate low-cost sensors (temperature/RH, smart plugs, vibration, webcams) and, if adopted, plan how their data will be retrieved and tied to run IDs.

- **Storage management**  
  Define retention/archival policy for `Z:\Logs` (e.g., move >18-month-old directories to compressed archive), monitor free space, and schedule housekeeping.

## Process & Governance

- **Pattern review cadence**  
  Set a routine (weekly/biweekly) to review `qc-unknown-patterns.log`, promote expected patterns into `expected-patterns.json`, and flag anomalies for engineering follow-up.

- **Stakeholder alignment**  
  Share progress with QA/operations leadership—confirm priority metrics, acceptable alert thresholds, and any compliance constraints around data retention.
