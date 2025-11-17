Hamilton Usage Dashboard (Read-Only)

Overview
- Small Flask app that reads MySQL (read-only) and shows which Hamiltons are currently in use.
- Sources:
  - lab_scheduler.instruments — authoritative list of Hamilton IDs (e.g., H3, H7, H14)
  - operation_data.instrument_ocupation — latest run entries per instrument
  - instrument_status is deprecated; not used.

Quick Start
- Python 3.10+ with mysql-connector-python installed.
- From `C:\QC`:
  - `python viz_hamilton_usage/app.py`
  - Open http://127.0.0.1:5000

Environment
- The app reads these env vars (with safe defaults):
  - QC_DB_HOST (default 192.168.60.4)
  - QC_DB_PORT (default 3307)
  - QC_DB_USER (default labsite)
  - QC_DB_PASSWORD (default vibrant)

Notes
- H14, H13, H7 are labeled as Test.
- “Busy” is inferred from the latest occupation row per instrument: finish_time is NULL or in the future; otherwise status hints, else idle/unknown.
- H3 shows alias ELISA_HAMILTON_1; H4 shows alias ELISA_HAMILTON_2.
- This is read-only and does not persist anything.

