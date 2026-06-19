#!/usr/bin/env bash
# Differential test: prove each refit hook behaves CORRECTLY in a state
# where the marketplace original behaves INCORRECTLY because of the
# transcript-lag bug.
#
# Method: build synthetic states where the ON-DISK JSONL transcript
# (what the marketplace hook reads) shows one thing, and the LIVE TASK
# STORE (what the refit reads) shows another. Pipe the same hook-input
# JSON to each hook; capture exit code + stderr; tabulate.
#
# Each test uses its own session_id to keep live-store state isolated.

set -u

WORK=$(mktemp -d -t differential-XXXXXX)
trap 'rm -rf "$WORK"' EXIT

MARKETPLACE_DIR="/home/claudeuser/.claude/plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples"
LOCAL_DIR="/home/claudeuser/.claude/hooks"

# ---------------------------------------------------------------------
# Test 1 — pre-commit-check-tasks.sh: same-turn TaskUpdate(completed)
# Transcript shows task#1 in_progress; live store shows completed.
# Expected: marketplace BLOCKS (exit 2), refit ALLOWS (exit 0).
# ---------------------------------------------------------------------

T1_SID="s-test1"
T1_TRANSCRIPT="$WORK/${T1_SID}.jsonl"
T1_TASKS_ROOT="$WORK/tasks-1"
mkdir -p "$T1_TASKS_ROOT/$T1_SID"

cat > "$T1_TRANSCRIPT" <<'EOF'
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"TaskCreate","input":{"subject":"canary"}}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"TaskUpdate","input":{"taskId":"1","status":"in_progress"}}]}}
EOF

cat > "$T1_TASKS_ROOT/$T1_SID/1.json" <<'EOF'
{"id":"1","subject":"canary","description":"","status":"completed","blocks":[],"blockedBy":[]}
EOF

T1_INPUT=$(jq -n --arg tp "$T1_TRANSCRIPT" \
    '{tool_name: "Bash", tool_input: {command: "git commit -m diff"}, transcript_path: $tp}')

echo "===================================================================="
echo "Test 1: pre-commit-check-tasks.sh — same-turn close + commit"
echo "===================================================================="
echo "Transcript shows task#1 in_progress; live store shows completed."
echo

echo "--- Marketplace ---"
M1_STDERR=$(echo "$T1_INPUT" | bash "$MARKETPLACE_DIR/pre-commit-check-tasks.sh" 2>&1 >/dev/null)
M1_RC=$?
echo "exit=$M1_RC"
echo "stderr: $M1_STDERR"
echo

echo "--- Local refit ---"
L1_STDERR=$(echo "$T1_INPUT" | CLAUDE_TASKS_ROOT="$T1_TASKS_ROOT" bash "$LOCAL_DIR/pre-commit-check-tasks.sh" 2>&1 >/dev/null)
L1_RC=$?
echo "exit=$L1_RC"
echo "stderr: $L1_STDERR"
echo

if [[ "$M1_RC" -eq 2 && "$L1_RC" -eq 0 ]]; then
    T1_VERDICT="PROVEN: marketplace blocked (rc=2), refit allowed (rc=0). Lag bug fixed."
elif [[ "$M1_RC" -eq 0 && "$L1_RC" -eq 0 ]]; then
    T1_VERDICT="INCONCLUSIVE: both allowed."
elif [[ "$M1_RC" -eq 2 && "$L1_RC" -eq 2 ]]; then
    T1_VERDICT="REFIT FAILED: both blocked. Refit did not read live store correctly."
else
    T1_VERDICT="UNEXPECTED: marketplace rc=$M1_RC, refit rc=$L1_RC."
fi
echo "VERDICT (Test 1): $T1_VERDICT"
echo

# ---------------------------------------------------------------------
# Test 2 — pre-task-blockedby-enforce.sh: same-turn addBlockedBy
# Transcript has TaskCreate calls but no addBlockedBy. Live store has
# task#3.blockedBy=["2"] and task#2 in_progress.
# Expected: marketplace ALLOWS (transcript has no blocker info),
# refit BLOCKS (live store knows the dependency).
# ---------------------------------------------------------------------

T2_SID="s-test2"
T2_TRANSCRIPT="$WORK/${T2_SID}.jsonl"
T2_TASKS_ROOT="$WORK/tasks-2"
mkdir -p "$T2_TASKS_ROOT/$T2_SID"

cat > "$T2_TRANSCRIPT" <<'EOF'
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"TaskCreate","input":{"subject":"blocker"}}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"TaskCreate","input":{"subject":"blocked"}}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"TaskUpdate","input":{"taskId":"2","status":"in_progress"}}]}}
EOF

cat > "$T2_TASKS_ROOT/$T2_SID/2.json" <<'EOF'
{"id":"2","subject":"blocker","description":"","status":"in_progress","blocks":["3"],"blockedBy":[]}
EOF

cat > "$T2_TASKS_ROOT/$T2_SID/3.json" <<'EOF'
{"id":"3","subject":"blocked","description":"","status":"pending","blocks":[],"blockedBy":["2"]}
EOF

T2_INPUT=$(jq -n --arg tp "$T2_TRANSCRIPT" \
    '{tool_name: "TaskUpdate", tool_input: {taskId: "3", status: "in_progress"}, transcript_path: $tp}')

echo "===================================================================="
echo "Test 2: pre-task-blockedby-enforce.sh — same-turn addBlockedBy missing in transcript"
echo "===================================================================="
echo "Transcript: TaskCreate(blocker), TaskCreate(blocked), TaskUpdate(2, in_progress)."
echo "Live store: #3.blockedBy=[\"2\"], #2 in_progress. (The addBlockedBy call"
echo "happened in this same turn and isn't flushed yet.)"
echo "Attempting TaskUpdate(3, in_progress)."
echo

echo "--- Marketplace ---"
M2_STDERR=$(echo "$T2_INPUT" | bash "$MARKETPLACE_DIR/pre-task-blockedby-enforce.sh" 2>&1 >/dev/null)
M2_RC=$?
echo "exit=$M2_RC"
echo "stderr (first 240 chars): ${M2_STDERR:0:240}"
echo

echo "--- Local refit ---"
L2_STDERR=$(echo "$T2_INPUT" | CLAUDE_TASKS_ROOT="$T2_TASKS_ROOT" bash "$LOCAL_DIR/pre-task-blockedby-enforce.sh" 2>&1 >/dev/null)
L2_RC=$?
echo "exit=$L2_RC"
echo "stderr (first 240 chars): ${L2_STDERR:0:240}"
echo

if [[ "$M2_RC" -eq 0 && "$L2_RC" -eq 2 ]]; then
    T2_VERDICT="PROVEN: marketplace falsely allowed (rc=0), refit correctly blocked (rc=2). Marketplace silently misses live-store blockers."
elif [[ "$M2_RC" -eq 2 && "$L2_RC" -eq 2 ]]; then
    T2_VERDICT="INCONCLUSIVE: both blocked."
elif [[ "$M2_RC" -eq 0 && "$L2_RC" -eq 0 ]]; then
    T2_VERDICT="REFIT FAILED: refit allowed despite live-store blocker."
else
    T2_VERDICT="UNEXPECTED: marketplace rc=$M2_RC, refit rc=$L2_RC."
fi
echo "VERDICT (Test 2): $T2_VERDICT"
echo

# ---------------------------------------------------------------------
# Test 3 — pre-commit-check-tasks.sh: empty transcript, live store has in_progress
# Marketplace reads empty transcript → counts 0 → ALLOWS (false negative).
# Refit reads live store → sees in_progress → BLOCKS.
# ---------------------------------------------------------------------

T3_SID="s-test3"
T3_TRANSCRIPT="$WORK/${T3_SID}.jsonl"
T3_TASKS_ROOT="$WORK/tasks-3"
mkdir -p "$T3_TASKS_ROOT/$T3_SID"
: > "$T3_TRANSCRIPT"

cat > "$T3_TASKS_ROOT/$T3_SID/1.json" <<'EOF'
{"id":"1","subject":"hidden","description":"","status":"in_progress","blocks":[],"blockedBy":[]}
EOF

T3_INPUT=$(jq -n --arg tp "$T3_TRANSCRIPT" \
    '{tool_name: "Bash", tool_input: {command: "git commit -m empty"}, transcript_path: $tp}')

echo "===================================================================="
echo "Test 3: pre-commit-check-tasks.sh — empty transcript, in_progress live store"
echo "===================================================================="
echo "Transcript: empty. Live store: task#1 in_progress."
echo "Marketplace can't see anything → ALLOWS commit."
echo "Refit reads live store → BLOCKS."
echo

echo "--- Marketplace ---"
M3_STDERR=$(echo "$T3_INPUT" | bash "$MARKETPLACE_DIR/pre-commit-check-tasks.sh" 2>&1 >/dev/null)
M3_RC=$?
echo "exit=$M3_RC"
echo "stderr: $M3_STDERR"
echo

echo "--- Local refit ---"
L3_STDERR=$(echo "$T3_INPUT" | CLAUDE_TASKS_ROOT="$T3_TASKS_ROOT" bash "$LOCAL_DIR/pre-commit-check-tasks.sh" 2>&1 >/dev/null)
L3_RC=$?
echo "exit=$L3_RC"
echo "stderr: $L3_STDERR"
echo

if [[ "$M3_RC" -eq 0 && "$L3_RC" -eq 2 ]]; then
    T3_VERDICT="PROVEN: marketplace falsely allowed (rc=0), refit correctly blocked (rc=2)."
elif [[ "$M3_RC" -eq 0 && "$L3_RC" -eq 0 ]]; then
    T3_VERDICT="INCONCLUSIVE: both allowed."
else
    T3_VERDICT="UNEXPECTED: marketplace rc=$M3_RC, refit rc=$L3_RC."
fi
echo "VERDICT (Test 3): $T3_VERDICT"
echo

# ---------------------------------------------------------------------
# Test 4 — pre-commit-check-tasks.sh: live store CORRECT in baseline case
# Both transcript and live store agree: 1 task in_progress.
# Expected: BOTH block (sanity check that refit doesn't false-positive).
# ---------------------------------------------------------------------

T4_SID="s-test4"
T4_TRANSCRIPT="$WORK/${T4_SID}.jsonl"
T4_TASKS_ROOT="$WORK/tasks-4"
mkdir -p "$T4_TASKS_ROOT/$T4_SID"

cat > "$T4_TRANSCRIPT" <<'EOF'
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"TaskCreate","input":{"subject":"baseline"}}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"TaskUpdate","input":{"taskId":"1","status":"in_progress"}}]}}
EOF

cat > "$T4_TASKS_ROOT/$T4_SID/1.json" <<'EOF'
{"id":"1","subject":"baseline","description":"","status":"in_progress","blocks":[],"blockedBy":[]}
EOF

T4_INPUT=$(jq -n --arg tp "$T4_TRANSCRIPT" \
    '{tool_name: "Bash", tool_input: {command: "git commit -m baseline"}, transcript_path: $tp}')

echo "===================================================================="
echo "Test 4 (baseline): both data sources agree — task#1 in_progress"
echo "===================================================================="
echo "Sanity check: refit must not over- or under-block when state is consistent."
echo

echo "--- Marketplace ---"
M4_STDERR=$(echo "$T4_INPUT" | bash "$MARKETPLACE_DIR/pre-commit-check-tasks.sh" 2>&1 >/dev/null)
M4_RC=$?
echo "exit=$M4_RC"
echo

echo "--- Local refit ---"
L4_STDERR=$(echo "$T4_INPUT" | CLAUDE_TASKS_ROOT="$T4_TASKS_ROOT" bash "$LOCAL_DIR/pre-commit-check-tasks.sh" 2>&1 >/dev/null)
L4_RC=$?
echo "exit=$L4_RC"
echo

if [[ "$M4_RC" -eq 2 && "$L4_RC" -eq 2 ]]; then
    T4_VERDICT="PASS: both correctly blocked. Refit matches marketplace on the agreement path."
elif [[ "$M4_RC" -eq 0 && "$L4_RC" -eq 0 ]]; then
    T4_VERDICT="FAIL: both wrongly allowed."
else
    T4_VERDICT="DIVERGED: marketplace rc=$M4_RC, refit rc=$L4_RC."
fi
echo "VERDICT (Test 4): $T4_VERDICT"
echo

# ---------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------
echo "===================================================================="
echo "SUMMARY"
echo "===================================================================="
printf "Test 1 (same-turn close + commit)       : %s\n" "$T1_VERDICT"
printf "Test 2 (same-turn addBlockedBy missing) : %s\n" "$T2_VERDICT"
printf "Test 3 (empty transcript live in_prog)  : %s\n" "$T3_VERDICT"
printf "Test 4 (baseline agreement)             : %s\n" "$T4_VERDICT"
echo
echo "Work dir: $WORK (cleaned on exit)"
