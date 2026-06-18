#!/usr/bin/env bash
# variant=local
# Stop hook: when Claude signals "plan complete / all gates passed" but a
# user-thrown gate was closed without captured evidence, block stop and
# require a final re-validation sweep.
#
# Add this to your project's .claude/settings.json (see README).
#
# ## What it does
#
# Fires on the Stop event. Two conditions must BOTH hold to block:
#
#   1. The last assistant message contains a "completion" keyword
#      (e.g. "plan complete", "implementation complete", "all gates passed",
#      "both gates passed", "all tasks complete", "plan 0–7 done").
#
#   2. The session has at least one completed task whose description is a
#      user-thrown gate (metadata `userGate: true`, OR metadata `tags`
#      contains `"user-gate"`, OR description carries the verbatim
#      "USER-ORDERED GATE" banner), AND no subsequent assistant message
#      surfaces explicit per-criterion proof (patterns like "AC:", "PROVEN BY").
#
# When both hold, the hook emits a blocking stderr message (exit 2) naming
# the gates that lack evidence. Claude must then produce the proof before
# stopping again.
#
# ## Why Stop (not PostToolUse)
#
# PostToolUse already has a sibling hook (post-task-complete-revalidate.sh)
# that catches individual closes. This Stop hook is the net underneath —
# it catches end-of-plan claims ("both gates passed") even when per-task
# closure moved through legitimate-looking paths.
#
# ## Escape hatch
#
# Set SUPERPOWERS_USERGATE_STOP_GUARD=0 to disable at runtime.

TRACE_LOG="${SUPERPOWERS_USERGATE_TRACE_LOG:-/tmp/claude-hooks/user-gate-trace.log}"
mkdir -p "$(dirname "$TRACE_LOG")" 2>/dev/null || true
trace() {
    local event="${1:-?}" reason="${2:-}"
    printf '%s | stop-revalidate | session=%s | %s%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${TRANSCRIPT_SHORT:-?}" "$event" \
        "${reason:+ | $reason}" >> "$TRACE_LOG" 2>/dev/null || true
}

HELPER_ROOT="${KINOFORGE_LOCAL_HOOKS_DIR:-$HOME/.claude/hooks}"

if [[ "${SUPERPOWERS_USERGATE_STOP_GUARD:-1}" == "0" ]]; then
    trace "skip" "stop-guard=0"
    exit 0
fi

# Fail-open: never cascade errors into the user's session.
trap 'trace "error" "trap-ERR"; exit 0' ERR

INPUT=$(cat)

TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
TRANSCRIPT_SHORT=$(basename "${TRANSCRIPT_PATH:-?}" .jsonl | cut -c1-8)
[[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]] && { trace "skip" "no-transcript"; exit 0; }

trace "enter"

SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
RESULT=$(python3 "$HELPER_ROOT/lib/tasks_live_query.py" \
    --session-id "$SESSION_ID" \
    --transcript "$TRANSCRIPT_PATH" 2>/dev/null)
[[ -z "$RESULT" ]] && { trace "skip" "no-result"; exit 0; }

# Get all completed tasks from the live store.
COMPLETED_TASKS=$(echo "$RESULT" | jq -r '.tasks[] | select(.status == "completed") | "\(.id)\t\(.description)"' 2>/dev/null)
TOTAL_TASKS=$(echo "$RESULT" | jq -r '.tasks | length // 0' 2>/dev/null || echo "0")

# Check the last assistant message in the transcript for completion keywords.
LAST_TEXT=$(jq -r '
    if .type == "assistant" then
        (.message.content // [])[] |
        if (type == "object") and .type == "text" and (.text | length) > 0
        then .text
        else empty
        end
    else empty
    end
' "$TRANSCRIPT_PATH" 2>/dev/null | tail -1 || echo "")

KEYWORDS=(
    "plan complete"
    "plan is complete"
    "plan finished"
    "implementation complete"
    "implementation is complete"
    "all tasks complete"
    "all tasks done"
    "all gates passed"
    "both gates passed"
    "both gates pass"
    "gate passed"
    "gate passes"
    "verification gate — passes"
    "verification gate passed"
    "plan 0"
    "tasks 0"
)

HAS_CLAIM="false"
LAST_LOWER=$(printf '%s' "$LAST_TEXT" | tr '[:upper:]' '[:lower:]')
for kw in "${KEYWORDS[@]}"; do
    if printf '%s' "$LAST_LOWER" | grep -qF "$kw"; then
        HAS_CLAIM="true"
        break
    fi
done

trace "scanned" "tasks=$TOTAL_TASKS claim=$HAS_CLAIM"

# If no completion claim, pass through.
if [[ "$HAS_CLAIM" != "true" ]]; then
    trace "pass" "no-completion-claim"
    exit 0
fi

# For each completed task, check if it is a user-gate and lacks evidence.
BLOCKED_GATES="[]"

while IFS=$'\t' read -r tid description; do
    [[ -z "$tid" ]] && continue

    # Classify as gate via json:metadata fence parse.
    META_JSON=$(printf '%s' "$description" | sed -n '/```json:metadata/,/```/{/```/d;p}' | head -50 | tr '\n' ' ')
    IS_GATE="false"

    if [[ -n "$META_JSON" ]]; then
        UG=$(printf '%s' "$META_JSON" | jq -r '.userGate // false' 2>/dev/null || echo "false")
        [[ "$UG" == "true" ]] && IS_GATE="true"
        if [[ "$IS_GATE" != "true" ]]; then
            HAS_TAG=$(printf '%s' "$META_JSON" | jq -r '(.tags // []) | any(. == "user-gate")' 2>/dev/null || echo "false")
            [[ "$HAS_TAG" == "true" ]] && IS_GATE="true"
        fi
    fi

    if [[ "$IS_GATE" != "true" ]]; then
        if printf '%s' "$description" | grep -qi "USER-ORDERED GATE"; then
            IS_GATE="true"
        fi
    fi

    [[ "$IS_GATE" != "true" ]] && continue

    # Look for per-criterion proof markers (AC: or PROVEN BY) in assistant
    # text after this task was closed. We scan the whole transcript for these
    # markers — the completion-keyword context implies the close is recent.
    PROOF_FOUND="false"
    if jq -r '
        if .type == "assistant" then
            (.message.content // [])[] |
            if (type == "object") and .type == "text"
            then .text
            else empty
            end
        else empty
        end
    ' "$TRANSCRIPT_PATH" 2>/dev/null | grep -qiE 'AC\s*:|PROVEN\s+BY'; then
        PROOF_FOUND="true"
    fi

    if [[ "$PROOF_FOUND" != "true" ]]; then
        GATE_SUBJECT=$(echo "$RESULT" | jq -r --arg tid "$tid" '.tasks[] | select(.id == $tid) | .subject' 2>/dev/null)
        GATE_SUBJECT="${GATE_SUBJECT:-?}"
        BLOCKED_GATES=$(printf '%s' "$BLOCKED_GATES" | jq -c \
            --arg id "$tid" --arg subject "$GATE_SUBJECT" \
            '. + [{"id": $id, "subject": $subject}]' 2>/dev/null || echo "$BLOCKED_GATES")
    fi
done <<< "$COMPLETED_TASKS"

BLOCKED_COUNT=$(printf '%s' "$BLOCKED_GATES" | jq -r 'length // 0' 2>/dev/null || echo "0")

trace "scanned" "tasks=$TOTAL_TASKS claim=$HAS_CLAIM blocked_gates=$BLOCKED_COUNT"

if [[ "${BLOCKED_COUNT:-0}" -le 0 ]]; then
    trace "pass" "no-unproven-gates"
    exit 0
fi

trace "block" "unproven_gates=$BLOCKED_COUNT"

{
    echo "PLAN-COMPLETE CLAIM DETECTED — SELF-ASSESS BEFORE STOPPING"
    echo
    echo "You signalled the plan / gates as complete, but the transcript shows"
    echo "$BLOCKED_COUNT user-thrown gate(s) closed without per-criterion proof in"
    echo "your subsequent text."
    echo
    echo "First — is this a hallucination or memory lapse? Check the transcript:"
    echo "  • Did you already post AC:/PROVEN BY evidence for these gates in"
    echo "    different wording? If yes, restate in the canonical shape so the"
    echo "    hook recognises it."
    echo "  • Did you genuinely verify these gates and forget to write the"
    echo "    evidence down? Then doing the verification again NOW is correct —"
    echo "    but do not fabricate evidence from memory."
    echo
    echo "If evidence is actually missing, and verifying is not currently"
    echo "possible (external system down, credentials missing, data unavailable),"
    echo "do NOT silently leave the claim standing. Either:"
    echo "  • Retract the completion claim in your next message, OR"
    echo "  • Raise the blocker to the user with AskUserQuestion — describe"
    echo "    what's missing and offer options (wait, skip with note, reshape)."
    echo
    echo "Gates missing evidence:"
    echo
    printf '%s' "$BLOCKED_GATES" | jq -r '.[] | "  - Task #" + .id + ": " + .subject' 2>/dev/null || true
    echo
    echo "Before stopping, reopen each listed gate and run /gate-check on it:"
    echo
    echo "    1. TaskUpdate taskId=<id> status=in_progress"
    echo "    2. /gate-check <id>"
    echo
    echo "/gate-check posts evidence in the shape this hook recognises:"
    echo
    echo "  Gate: <subject>"
    echo "  AC: <criterion> — PROVEN BY <exact command/output/subagent result>"
    echo "  AC: <criterion> — PROVEN BY <...>"
    echo
    echo "If /gate-check is not installed, post the AC: lines inline by running"
    echo "the verification yourself. If a gate cannot be proven right now,"
    echo "reopen it and retract the completion claim above."
    echo "(To disable this check, set SUPERPOWERS_USERGATE_STOP_GUARD=0.)"
} >&2

exit 2
