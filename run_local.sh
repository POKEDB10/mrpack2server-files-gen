#!/bin/bash
# Linux/Mac script to run the app locally

export RUNNING_LOCALLY=1
export PRIMARY_WORKER=1
export PORT=${PORT:-8090}
export DEBUG=${DEBUG:-True}
export HOST=${HOST:-127.0.0.1}

echo "Starting local development server..."
python3 run_local.py

