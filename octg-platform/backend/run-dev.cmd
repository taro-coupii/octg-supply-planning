@echo off
REM Dev backend launcher.
REM
REM Two things here are deliberate and documented in HANDOFF.md:
REM   * DATABASE_URL must be set explicitly -- app/db.py defaults to Postgres,
REM     and this environment runs against the SQLite dev.db.
REM   * NO --reload. WatchFiles is unreliable on this machine: it detects the
REM     change, tries to restart, never prints "Started server process", and
REM     hangs. Backend changes need a manual stop and start.
cd /d "%~dp0"
set DATABASE_URL=sqlite:///./dev.db
".\.venv\Scripts\python.exe" -m uvicorn app.main:app --port 8000
