#!/usr/bin/env bash
# Status snapshot of the running uptime-field hourly sweep.
# Run: bash tools/uptime_sweep_status.sh
set -u
LOG=tools/_uptime_field_sweep_log.jsonl
ERR=tools/_uptime_field_sweep_errors.log

echo "=== process ==="
pgrep -af 'uptime_field_hourly_sweep' || echo "(no sweep process running)"

echo
echo "=== iterations completed ==="
if [[ -f "$LOG" ]]; then
  COUNT=$(wc -l < "$LOG")
  echo "$COUNT of 16"
else
  echo "(log file not created yet)"
fi

echo
echo "=== per-iteration summary ==="
if [[ -f "$LOG" ]]; then
  python3 tools/uptime_sweep_summary.py
fi

echo
echo "=== errors ==="
if [[ -f "$ERR" ]]; then
  cat "$ERR"
else
  echo "(none)"
fi
