#!/usr/bin/env bash
# variant=local
# PostToolUse hook on Agent: validate the subagent's return content against
# the currently in_progress task's evidence axes. Fires right after Agent
# tool_result arrives — before the coordinator absorbs and reports upward.
#
# If the in_progress task carries metadata `requireEvidenceTokens` (or the
# `requireABCompare: true` shortcut) and the subagent's report lacks tokens
# from one or more axes, this hook blocks with a stderr that names the
# missing axes. Forces the coordinator to re-dispatch on the spot rather
# than grind through "looks good" at task-close time.
#
# Opt in; SUPERPOWERS_AGENT_RETURN_GUARD=0 disables.

TRACE_LOG="${SUPERPOWERS_USERGATE_TRACE_LOG:-/tmp/claude-hooks/user-gate-trace.log}"
mkdir -p "$(dirname "$TRACE_LOG")" 2>/dev/null || true
trace() {
    local tid="${1:-?}" event="${2:-?}" reason="${3:-}"
    printf '%s | post-agent | task=%s | %s%s | variant=local\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tid" "$event" \
        "${reason:+ | $reason}" >> "$TRACE_LOG" 2>/dev/null || true
}

HELPER_ROOT="${KINOFORGE_LOCAL_HOOKS_DIR:-$HOME/.claude/hooks}"

if [[ "${SUPERPOWERS_AGENT_RETURN_GUARD:-1}" == "0" ]]; then
    trace "?" "skip" "guard=0"
    exit 0
fi

trap 'trace "?" "error" "trap-ERR"; exit 0' ERR

INPUT=$(cat)

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$TOOL_NAME" != "Agent" ]] && { trace "?" "skip" "tool=$TOOL_NAME"; exit 0; }

RESPONSE=$(echo "$INPUT" | jq -r '.tool_response // .tool_result // empty' 2>/dev/null)
[[ -z "$RESPONSE" ]] && { trace "?" "skip" "no-response"; exit 0; }

TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
[[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]] && { trace "?" "skip" "no-transcript"; exit 0; }

SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
RESULT=$(python3 "$HELPER_ROOT/lib/tasks_live_query.py" \
    --session-id "$SESSION_ID" \
    --transcript "$TRANSCRIPT_PATH" 2>/dev/null)
[[ -z "$RESULT" ]] && exit 0

# Find the currently in_progress task.
IN_PROGRESS=$(echo "$RESULT" | jq -r '.tasks[] | select(.status == "in_progress") | "\(.id)\t\(.description)"' 2>/dev/null | head -1)
[[ -z "$IN_PROGRESS" ]] && { trace "?" "pass" "no-inprogress-task"; exit 0; }

TASK_ID=$(printf '%s' "$IN_PROGRESS" | cut -f1)
DESCRIPTION=$(printf '%s' "$IN_PROGRESS" | cut -f2-)
SUBJECT=$(echo "$RESULT" | jq -r --arg tid "$TASK_ID" '.tasks[] | select(.id == $tid) | .subject' 2>/dev/null)
SUBJECT="${SUBJECT:-?}"

# Parse json:metadata fence from description to extract evidence axes.
META_JSON=$(printf '%s' "$DESCRIPTION" | sed -n '/```json:metadata/,/```/{/```/d;p}' | head -50 | tr '\n' ' ')

AXES_JSON="[]"
if [[ -n "$META_JSON" ]]; then
    RAW_AXES=$(printf '%s' "$META_JSON" | jq -c '.requireEvidenceTokens // null' 2>/dev/null || echo "null")
    if [[ "$RAW_AXES" != "null" ]]; then
        AXES_JSON="$RAW_AXES"
    else
        AB_SHORTCUT=$(printf '%s' "$META_JSON" | jq -r '.requireABCompare // false' 2>/dev/null || echo "false")
        if [[ "$AB_SHORTCUT" == "true" ]]; then
            AXES_JSON='[["baseline","old","before","v0","v1","iter-0","iter0","original","pre"],["new","refactored","after","v2","iter-1","iter1","post","updated","replacement"]]'
        fi
    fi
fi

AXES_COUNT=$(printf '%s' "$AXES_JSON" | jq -r 'length // 0' 2>/dev/null || echo "0")

# No in_progress task with axes → nothing to enforce.
[[ "${AXES_COUNT:-0}" -le 0 ]] && { trace "$TASK_ID" "pass" "no-axes-for-inprogress"; exit 0; }

trace "$TASK_ID" "parsed" "axes=$AXES_COUNT subject='$SUBJECT'"

# Check the subagent return against each axis using grep pattern matching.
MISSING_JSON="[]"
for i in $(seq 0 $((AXES_COUNT - 1))); do
    AXIS_TOKENS=$(printf '%s' "$AXES_JSON" | jq -c ".[$i]" 2>/dev/null || echo "[]")
    TOKENS=$(printf '%s' "$AXIS_TOKENS" | jq -r '.[] // empty' 2>/dev/null | sed 's/[][\\/.+*?^$|(){}]/\\&/g' | grep -v '^$' || true)
    PATTERN=$(printf '%s' "$TOKENS" | tr '\n' '|' | sed 's/|$//')
    if [[ -z "$PATTERN" ]]; then
        MISSING_JSON=$(printf '%s' "$MISSING_JSON" | jq -c \
            --argjson idx "$i" --argjson tokens "$AXIS_TOKENS" \
            '. + [{"index": $idx, "tokens": $tokens}]' 2>/dev/null || echo "$MISSING_JSON")
    elif ! printf '%s' "$RESPONSE" | grep -qiE "\\b(${PATTERN})\\b"; then
        MISSING_JSON=$(printf '%s' "$MISSING_JSON" | jq -c \
            --argjson idx "$i" --argjson tokens "$AXIS_TOKENS" \
            '. + [{"index": $idx, "tokens": $tokens}]' 2>/dev/null || echo "$MISSING_JSON")
    fi
done

MISSING_COUNT=$(printf '%s' "$MISSING_JSON" | jq -r 'length // 0' 2>/dev/null || echo "0")

if [[ "${MISSING_COUNT:-0}" -le 0 ]]; then
    trace "$TASK_ID" "pass" "subagent-return-covers-axes"
    exit 0
fi

trace "$TASK_ID" "block" "subagent-return-missing-axes count=$MISSING_COUNT"

{
    echo "SUBAGENT RETURN DOES NOT COVER DECLARED EVIDENCE AXES"
    echo
    echo "Task #$TASK_ID ('$SUBJECT') is in_progress and its metadata declares"
    echo "evidence axes the subagent's report was expected to cover. The"
    echo "returned content is missing a token from these axes:"
    echo
    printf '%s' "$MISSING_JSON" | jq -r '.[] | "  axis #\(.index): none of " + (.tokens | join(" | ")) + " appeared"' 2>/dev/null || true
    echo
    echo "This is not a task close — this is the subagent's report you were"
    echo "about to absorb. Either:"
    echo "  1. Re-dispatch the subagent with an explicit instruction to report"
    echo "     observations from every missing axis."
    echo "  2. If the subagent genuinely did not observe one side (e.g. the"
    echo "     baseline run failed and no output exists), dispatch a SECOND"
    echo "     subagent to specifically produce the missing observation."
    echo "  3. If the axis set is wrong for this task (bad plan metadata),"
    echo "     update requireEvidenceTokens via TaskUpdate before continuing —"
    echo "     transparently, not as a bypass."
    echo
    echo "Do NOT proceed to absorb this report and close the task on partial"
    echo "evidence. post-task-complete-revalidate will catch it at close time,"
    echo "but a re-dispatch now is cheaper than a reopen later."
    echo
    echo "(Runtime disable: SUPERPOWERS_AGENT_RETURN_GUARD=0. Trace: $TRACE_LOG)"
} >&2

exit 2
