#!/usr/bin/env bash
# Written in 2026 at JPL by Emmy Killett (she/her) and Claude Opus 4.7 adaptive (it/its).
set -euo pipefail

input=$(cat)
transcript=$(echo "$input" | jq -r '.transcript_path')

# Prefer LOOP_ITERATION from the outer loop; fall back to next free number.
if [[ -n "${LOOP_ITERATION:-}" ]]; then
  iter="$LOOP_ITERATION"
else
  n=1
  while [[ -e "PROGRESS$(printf '%03d' "$n").md" ]]; do ((n++)); done
  iter=$(printf '%03d' "$n")
fi
outfile="PROGRESS${iter}.md"

# Headless sub-session generates the summary from the transcript.
# Timeout prevents indefinite hangs (rate limit, network failure, etc.).
if timeout 120 claude -p "Read the transcript JSONL at $transcript and write a \
concise progress report to $outfile in the current directory, covering: \
current task, completed work, pending work, files modified, and the \
exact next step to resume. Be specific enough that a fresh session can \
continue without re-reading the transcript." >/dev/null 2>&1 && [[ -s "$outfile" ]]; then
  # Progress saved successfully — kill the outer session so the loop advances.
  kill -TERM "$PPID" 2>/dev/null || true
  echo "Threshold reached — progress saved to $outfile" >&2
  exit 2
else
  echo "WARNING: Failed to save progress to $outfile — letting Claude continue." >&2
  exit 1
fi
