@echo off
REM Windows batch script to run the app locally
set RUNNING_LOCALLY=1
set PRIMARY_WORKER=1
set PORT=8090
set DEBUG=True
set HOST=127.0.0.1

echo Starting local development server...
python run_local.py

