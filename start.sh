#!/bin/sh
set -e
PORT="${PORT:-8080}"
echo "Starting HGU Test on port ${PORT}"
exec gunicorn -b "0.0.0.0:${PORT}" -w 1 --timeout 120 --access-logfile - --error-logfile - app:app
