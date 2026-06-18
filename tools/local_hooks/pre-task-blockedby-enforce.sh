#!/usr/bin/env bash
# variant=local
# PreToolUse hook: block TaskUpdate(status=in_progress) when the task's
# blockedBy list still points at uncompleted tasks. Skill prose that says
# "respect dependencies" is not enforcement — this hook is.
#
# ## What it does
#
# Fires on PreToolUse with matcher=TaskUpdate. When status=in_progress:
#   1. Query tasks_live_query.py for live task state.
#   2. If the target taskId has any blockedBy entry whose current status
#      is not "completed", refuse with exit=2 and stderr listing blockers.
#   3. All other TaskUpdate shapes (status=pending|cancelled|deleted|
#      completed, description/priority updates) pass through.
#
# ## Why PreToolUse (not Post)
#
# Starting work on a task without its prerequisites is the actual damage.
# PreToolUse refuses the transition; the coordinator has to close the
# blockers first or explain why they're irrelevant by closing them as
# cancelled/deleted.
#
# ## Escape hatch
#
# Set SUPERPOWERS_BLOCKEDBY_GUARD=0 to disable at runtime. Useful when
# plans are hand-reordered or when a coordinator explicitly wants to
# parallelize past a declared dependency.
#
# ## Trace log
#
# Every decision writes a line to
# /tmp/claude-hooks/user-gate-trace.log (override via
# SUPERPOWERS_USERGATE_TRACE_LOG). Tail with:
#   tail -F /tmp/claude-hooks/user-gate-trace.log

TRACE_LOG="${SUPERPOWERS_USERGATE_TRACE_LOG:-/tmp/claude-hooks/user-gate-trace.log}"
mkdir -p "$(dirname "$TRACE_LOG")" 2>/dev/null || true
trace() {
    local tid="${1:-?}" event="${2:-?}" reason="${3:-}"
    printf '%s | pre-blockedby | task=%s | %s%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tid" "$event" \
        "${reason:+ | $reason}" >> "$TRACE_LOG" 2>/dev/null || true
}

ALLOW='{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}'
HELPER_ROOT="${KINOFORGE_LOCAL_HOOKS_DIR:-$HOME/.claude/hooks}"

if [[ "${SUPERPOWERS_BLOCKEDBY_GUARD:-1}" == "0" ]]; then
    trace "?" "skip" "guard=0"
    echo "$ALLOW"; exit 0
fi

# Fail-open.
trap 'trace "?" "error" "trap-ERR"; echo "$ALLOW"; exit 0' ERR

INPUT=$(cat)

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$TOOL_NAME" != "TaskUpdate" ]] && { trace "?" "skip" "tool=$TOOL_NAME"; echo "$ALLOW"; exit 0; }

STATUS=$(echo "$INPUT" | jq -r '.tool_input.status // empty' 2>/dev/null)
[[ "$STATUS" != "in_progress" ]] && { trace "?" "skip" "status=$STATUS"; echo "$ALLOW"; exit 0; }

TASK_ID=$(echo "$INPUT" | jq -r '.tool_input.taskId // empty' 2>/dev/null)
[[ -z "$TASK_ID" ]] && { trace "?" "skip" "no-task-id"; echo "$ALLOW"; exit 0; }

TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
[[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]] && { trace "$TASK_ID" "skip" "no-transcript"; echo "$ALLOW"; exit 0; }

trace "$TASK_ID" "enter" "status=in_progress"

SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
RESULT=$(python3 "$HELPER_ROOT/lib/tasks_live_query.py" \
    --session-id "$SESSION_ID" \
    --transcript "$TRANSCRIPT_PATH" 2>/dev/null)
[[ -z "$RESULT" ]] && { echo "$ALLOW"; exit 0; }

# Build STATUS_OF (id → status) and BLOCKED_BY (id → comma list) from RESULT.
STATUS_OF=$(echo "$RESULT" | jq -r '.tasks[] | "\(.id):\(.status)"' 2>/dev/null)
BLOCKED_BY=$(echo "$RESULT" | jq -r '.blocked_by_lookup // {} | to_entries[] | "\(.key):\(.value | join(","))"' 2>/dev/null)

TARGET_SUBJECT=$(echo "$RESULT" | jq -r --arg tid "$TASK_ID" '.tasks[] | select(.id == $tid) | .subject' 2>/dev/null)
TARGET_SUBJECT="${TARGET_SUBJECT:-?}"

# Find the blockedBy entries for TASK_ID.
ALL_BLOCKERS=$(echo "$BLOCKED_BY" | grep "^${TASK_ID}:" | sed "s/^${TASK_ID}://" | head -1)

MISSING_COUNT=0
MISSING_JSON="[]"
if [[ -n "$ALL_BLOCKERS" ]]; then
    # For each blocker, check if its status is completed.
    MISSING_JSON=$(
        echo "$ALL_BLOCKERS" | tr ',' '\n' | while IFS= read -r bid; do
            [[ -z "$bid" ]] && continue
            BSTATUS=$(echo "$STATUS_OF" | grep "^${bid}:" | sed "s/^${bid}://" | head -1)
            BSTATUS="${BSTATUS:-unknown}"
            BSUBJECT=$(echo "$RESULT" | jq -r --arg bid "$bid" '.tasks[] | select(.id == $bid) | .subject' 2>/dev/null)
            BSUBJECT="${BSUBJECT:-?}"
            if [[ "$BSTATUS" != "completed" ]]; then
                jq -n --arg id "$bid" --arg subject "$BSUBJECT" --arg status "$BSTATUS" \
                    '{id:$id,subject:$subject,status:$status}'
            fi
        done | jq -s '.'
    )
    MISSING_COUNT=$(echo "$MISSING_JSON" | jq -r 'length // 0' 2>/dev/null)
fi

trace "$TASK_ID" "scanned" "blockers=[$ALL_BLOCKERS] missing=$MISSING_COUNT subject='$TARGET_SUBJECT'"

if [[ "${MISSING_COUNT:-0}" -le 0 ]]; then
    trace "$TASK_ID" "pass" "blockers-cleared-or-none"
    echo "$ALLOW"; exit 0
fi

trace "$TASK_ID" "block" "missing=$MISSING_COUNT"

{
    echo "BLOCKED-BY DEPENDENCY NOT COMPLETED — SELF-ASSESS BEFORE PROCEEDING"
    echo
    echo "You tried to move Task #$TASK_ID ('$TARGET_SUBJECT') into in_progress,"
    echo "but its blockedBy list still points at tasks that are not completed:"
    echo
    echo "$MISSING_JSON" | jq -r '.[] | "  - Task #" + .id + " [" + .status + "]: " + .subject' 2>/dev/null || true
    echo
    echo "This is not an automatic refusal. Before reacting, pause and ASSESS."
    echo
    echo "STEP 1 — Check your own read. Is this a real blocker or did you miss"
    echo "something?"
    echo "  • Did you actually need the blocker's output for THIS task? Re-read"
    echo "    this task's Goal/AC — are you sure the output of the listed"
    echo "    blocker(s) is genuinely required as input?"
    echo "  • Did the blocker's work get done informally in a previous turn"
    echo "    (e.g. you ran the same investigation inline without closing the"
    echo "    task)? If yes, close the blocker properly with TaskUpdate"
    echo "    status=completed AND post the evidence for it — then retry."
    echo "  • Did a recent plan revision render the blocker obsolete? If yes,"
    echo "    TaskUpdate the blocker to status=cancelled with a one-line reason"
    echo "    in the description — then retry."
    echo
    echo "STEP 2 — If it IS a real blocker, choose one:"
    echo "  (a) Do the blocker's work first, close it with evidence, then"
    echo "      retry THIS task. This is the default."
    echo "  (b) If the blocker genuinely cannot be done right now (missing"
    echo "      input, waiting on external data), RAISE IT TO THE USER with"
    echo "      AskUserQuestion — describe the blocker, what you'd need, and"
    echo "      offer options (wait / skip with justification / remove from"
    echo "      plan). Let the user decide the plan reshape."
    echo "  (c) If you're certain the plan's declared ordering is wrong for"
    echo "      this situation, state that explicitly — quote the reasoning"
    echo "      and ask the user to confirm removing the blockedBy link."
    echo
    echo "What NOT to do: do NOT close the blocker with status=completed"
    echo "without actually doing its work just to bypass this hook."
    echo
    echo "Hallucination check: if you are surprised this task has a blockedBy"
    echo "at all, verify with TaskList — your mental model may have drifted"
    echo "from the persisted state."
    echo
    echo "(Runtime disable: SUPERPOWERS_BLOCKEDBY_GUARD=0. Trace log:"
    echo " $TRACE_LOG)"
} >&2

exit 2
