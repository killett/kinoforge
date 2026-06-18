#!/usr/bin/env bash
# variant=local
# PostToolUse hook: when a USER-THROWN gate task is closed, force Claude to
# re-state evidence before moving on.
#
# Add this to your project's .claude/settings.json (see README).
#
# ## What it does
#
# Triggers on TaskUpdate tool calls with status=completed. Queries
# tasks_live_query.py for the task's description and parses the embedded
# `json:metadata` fence. If metadata says the task is a user-thrown gate —
# `userGate: true` OR `tags` contains `"user-gate"` — emits a blocking
# reminder (exit 2 + stderr) that forces Claude to confirm every
# acceptanceCriteria with concrete evidence in the next turn.
#
# Regular (non-gate) tasks pass through silently.
#
# ## Why PostToolUse (not PreToolUse)
#
# The close itself is allowed — a user-gate task *can* legitimately be
# completed. What the hook protects against is closing-and-moving-on
# without proof. PostToolUse fires after the tool succeeds, so the block
# is a system-reminder the model MUST address before its next action,
# not a refusal to close the task.
#
# ## Escape hatch
#
# Set SUPERPOWERS_USERGATE_GUARD=0 to disable at runtime. The hook is
# opt-in already, so an escape hatch exists mainly for subagent contexts
# where re-validation has already happened upstream.

# Trace logging — every decision point writes one line to the shared trace
# log. Tail with: tail -F /tmp/claude-hooks/user-gate-trace.log
TRACE_LOG="${SUPERPOWERS_USERGATE_TRACE_LOG:-/tmp/claude-hooks/user-gate-trace.log}"
mkdir -p "$(dirname "$TRACE_LOG")" 2>/dev/null || true
trace() {
    # Args: task_id event reason
    local tid="${1:-?}" event="${2:-?}" reason="${3:-}"
    printf '%s | post-complete | task=%s | %s%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tid" "$event" \
        "${reason:+ | $reason}" >> "$TRACE_LOG" 2>/dev/null || true
}

HELPER_ROOT="${KINOFORGE_LOCAL_HOOKS_DIR:-$HOME/.claude/hooks}"

if [[ "${SUPERPOWERS_USERGATE_GUARD:-1}" == "0" ]]; then
    trace "?" "skip" "guard=0"
    exit 0
fi

# Fail-open: if anything unexpected breaks, never block.
trap 'trace "?" "error" "trap-ERR"; exit 0' ERR

INPUT=$(cat)

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$TOOL_NAME" != "TaskUpdate" ]] && { trace "?" "skip" "tool=$TOOL_NAME"; exit 0; }

STATUS=$(echo "$INPUT" | jq -r '.tool_input.status // empty' 2>/dev/null)
[[ "$STATUS" != "completed" ]] && { trace "?" "skip" "status=$STATUS"; exit 0; }

TASK_ID=$(echo "$INPUT" | jq -r '.tool_input.taskId // empty' 2>/dev/null)
[[ -z "$TASK_ID" ]] && { trace "?" "skip" "no-task-id"; exit 0; }

trace "$TASK_ID" "enter" "status=completed"

TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
[[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]] && { trace "$TASK_ID" "skip" "no-transcript"; exit 0; }

SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
RESULT=$(python3 "$HELPER_ROOT/lib/tasks_live_query.py" \
    --session-id "$SESSION_ID" \
    --transcript "$TRANSCRIPT_PATH" 2>/dev/null)
[[ -z "$RESULT" ]] && { trace "$TASK_ID" "skip" "no-result"; exit 0; }

CLOSED_TASK_ID="$TASK_ID"
DESCRIPTION=$(echo "$RESULT" | jq -r ".tasks[] | select(.id == \"$CLOSED_TASK_ID\") | .description" 2>/dev/null)
SUBJECT=$(echo "$RESULT" | jq -r ".tasks[] | select(.id == \"$CLOSED_TASK_ID\") | .subject" 2>/dev/null)
SUBJECT="${SUBJECT:-?}"

# Parse the json:metadata code fence inside the task description.
# Extract the fence content and parse with jq.
META_JSON=$(printf '%s' "$DESCRIPTION" | sed -n '/```json:metadata/,/```/{/```/d;p}' | head -50 | tr '\n' ' ')

USER_GATE_FLAG="false"
TAGS_LIST=""
AC_COUNT=0
CRITERIA_JSON="[]"
AB_REQUIRED="false"
AB_AXES="[]"

if [[ -n "$META_JSON" ]]; then
    USER_GATE_FLAG=$(printf '%s' "$META_JSON" | jq -r '.userGate // false' 2>/dev/null || echo "false")
    TAGS_LIST=$(printf '%s' "$META_JSON" | jq -r '(.tags // []) | join(",")' 2>/dev/null || echo "")
    CRITERIA_JSON=$(printf '%s' "$META_JSON" | jq -c '.acceptanceCriteria // []' 2>/dev/null || echo "[]")
    AC_COUNT=$(printf '%s' "$CRITERIA_JSON" | jq -r 'length // 0' 2>/dev/null || echo "0")

    # A/B axes
    RAW_AXES=$(printf '%s' "$META_JSON" | jq -c '.requireEvidenceTokens // null' 2>/dev/null || echo "null")
    if [[ "$RAW_AXES" != "null" ]]; then
        AB_REQUIRED="true"
        AB_AXES="$RAW_AXES"
    else
        AB_SHORTCUT=$(printf '%s' "$META_JSON" | jq -r '.requireABCompare // false' 2>/dev/null || echo "false")
        if [[ "$AB_SHORTCUT" == "true" ]]; then
            AB_REQUIRED="true"
            AB_AXES='[["baseline","old","before","v0","v1","iter-0","iter0","original","pre"],["new","refactored","after","v2","iter-1","iter1","post","updated","replacement"]]'
        fi
    fi
fi

# Also count as gate if description carries the verbatim USER-ORDERED GATE banner.
if printf '%s' "$DESCRIPTION" | grep -qi "USER-ORDERED GATE"; then
    USER_GATE_FLAG="true"
fi

# Scan transcript for evidence signals.
# Find the line index where this task last went in_progress (by scanning for
# TaskUpdate(taskId=TASK_ID, status=in_progress) in the transcript).
SCAN_FROM_LINE=0
TOTAL_LINES=$(wc -l < "$TRANSCRIPT_PATH" 2>/dev/null || echo "0")

# Walk transcript lines to find in_progress marker and collect text content.
# We do this with jq applied to the entire file.
EVIDENCE_ON_RECORD="false"
USER_VERIFY="false"
AGENT_ASSESS="false"
LAST_AGENT_PREVIEW=""
AB_SATISFIED="false"
AB_MISSING_JSON="[]"

# Build a consolidated evidence structure from the transcript.
# Extract: (1) last in_progress line for this task, (2) assistant texts,
# (3) user texts, (4) tool_result texts — all indexed by line position.
# We use jq to parse the JSONL and output tabular data.

# Find the last line index where this task went in_progress.
IN_PROGRESS_LINE=$(jq -r --arg tid "$TASK_ID" '
    if .type == "assistant" then
        (.message.content // [])[] |
        if (type == "object") and .type == "tool_use" and .name == "TaskUpdate"
            and (.input.taskId | tostring) == $tid
            and .input.status == "in_progress"
        then "FOUND"
        else empty
        end
    else empty
    end
' "$TRANSCRIPT_PATH" 2>/dev/null | tail -1 || echo "")

# Get the line number of the last in_progress marker via grep approach.
# We scan the raw JSONL for the pattern — approximate but sufficient.
if [[ -n "$IN_PROGRESS_LINE" ]]; then
    SCAN_FROM_LINE=$(grep -n "\"in_progress\"" "$TRANSCRIPT_PATH" 2>/dev/null | \
        grep "\"taskId\":\"${TASK_ID}\"\\|\"taskId\": \"${TASK_ID}\"" | \
        tail -1 | cut -d: -f1 || echo "0")
    SCAN_FROM_LINE="${SCAN_FROM_LINE:-0}"
fi

# Extract assistant text messages after SCAN_FROM_LINE.
ASSISTANT_TEXTS=$(awk -v from="$SCAN_FROM_LINE" 'NR > from' "$TRANSCRIPT_PATH" 2>/dev/null | \
    jq -r '
        if .type == "assistant" then
            (.message.content // [])[] |
            if (type == "object") and .type == "text" and (.text | length) > 0
            then .text
            else empty
            end
        else empty
        end
    ' 2>/dev/null || echo "")

# Extract tool_result content after SCAN_FROM_LINE.
TOOL_RESULT_TEXTS=$(awk -v from="$SCAN_FROM_LINE" 'NR > from' "$TRANSCRIPT_PATH" 2>/dev/null | \
    jq -r '
        if .type == "user" then
            (.message.content // [])[] |
            if (type == "object") and .type == "tool_result" then
                if (.content | type) == "string" then .content
                elif (.content | type) == "array" then
                    .content[] | if (type == "object") and .type == "text" then .text else empty end
                else empty
                end
            else empty
            end
        else empty
        end
    ' 2>/dev/null || echo "")

# Check for AC:/PROVEN BY evidence markers.
if printf '%s' "$ASSISTANT_TEXTS" | grep -qiE 'AC\s*:|PROVEN\s+BY'; then
    EVIDENCE_ON_RECORD="true"
fi

# User-verification check: did user send a real message in window since in_progress?
USER_TEXTS_IN_WINDOW=$(awk -v from="$SCAN_FROM_LINE" 'NR > from' "$TRANSCRIPT_PATH" 2>/dev/null | \
    jq -r '
        if .type == "user" then
            .message.content |
            if type == "string" and length > 0 then .
            elif type == "array" then
                .[] | if (type == "object") and .type == "text" then .text else empty end
            else empty
            end
        else empty
        end
    ' 2>/dev/null | grep -v '^$' | head -1 || echo "")

if [[ -n "$USER_TEXTS_IN_WINDOW" ]]; then
    USER_VERIFY="true"
fi

# Agent assessment keyword check.
ASSESS_PATTERN='verified|confirmed|tested|checked|passed|success|succeeded|result|output|works?|working|acceptance|criterion|criteria|proven|observed|shows?|displayed|returned|exit\s*0|all\s+green|done|complete|built|created|written|wrote|assess(ed|ment)?|closed|closure|executed|answered|repl(y|ied)|responded|captured|recorded|logged|dispatched|spawned|invoked|inspected|parsed|scanned|measured|examined|reviewed|audited|summary|summari[sz]ed|finished|finali[sz]ed|legitimate|merged|committed|pushed|patched|implemented|validated'

# Add user-extensible keywords.
EXTRA_KW="${SUPERPOWERS_USERGATE_KEYWORDS:-}"
if [[ -n "$EXTRA_KW" ]]; then
    EXTRA_PATTERN=$(printf '%s' "$EXTRA_KW" | tr ',' '|' | sed 's/[[:space:]]//g')
    ASSESS_PATTERN="${ASSESS_PATTERN}|${EXTRA_PATTERN}"
fi

ALL_WINDOW_TEXT=$(printf '%s\n%s' "$ASSISTANT_TEXTS" "$TOOL_RESULT_TEXTS")
if printf '%s' "$ALL_WINDOW_TEXT" | grep -qiE "$ASSESS_PATTERN"; then
    AGENT_ASSESS="true"
fi

# Last agent text preview (first 240 chars of last assistant text).
if [[ -n "$ASSISTANT_TEXTS" ]]; then
    LAST_AGENT_PREVIEW=$(printf '%s' "$ASSISTANT_TEXTS" | tail -1 | cut -c1-240)
fi

# A/B axis enforcement.
if [[ "$AB_REQUIRED" == "true" ]]; then
    AB_MISSING_JSON="[]"
    AXES_COUNT=$(printf '%s' "$AB_AXES" | jq -r 'length // 0' 2>/dev/null || echo "0")
    for i in $(seq 0 $((AXES_COUNT - 1))); do
        AXIS_TOKENS=$(printf '%s' "$AB_AXES" | jq -c ".[$i]" 2>/dev/null || echo "[]")
        PATTERN=$(printf '%s' "$AXIS_TOKENS" | jq -r 'map(.) | join("|")' 2>/dev/null || echo "")
        if [[ -n "$PATTERN" ]]; then
            if ! printf '%s' "$ALL_WINDOW_TEXT" | grep -qiE "\\b(${PATTERN})\\b"; then
                AB_MISSING_JSON=$(printf '%s' "$AB_MISSING_JSON" | jq -c \
                    --argjson idx "$i" --argjson tokens "$AXIS_TOKENS" \
                    '. + [{"index": $idx, "tokens": $tokens}]' 2>/dev/null || echo "$AB_MISSING_JSON")
            fi
        fi
    done
    AB_MISSING_COUNT=$(printf '%s' "$AB_MISSING_JSON" | jq -r 'length // 0' 2>/dev/null || echo "0")
    if [[ "${AB_MISSING_COUNT:-0}" -le 0 ]]; then
        AB_SATISFIED="true"
        AGENT_ASSESS="true"
    fi
fi

trace "$TASK_ID" "parsed" "userGate=$USER_GATE_FLAG tags=[$TAGS_LIST] ac=$AC_COUNT evidence=$EVIDENCE_ON_RECORD user_verify=$USER_VERIFY agent_assess=$AGENT_ASSESS ab_req=$AB_REQUIRED ab_ok=$AB_SATISFIED"

# Evidence-axis enforcement: each declared axis must show at least one token.
if [[ "$AB_REQUIRED" == "true" && "$AB_SATISFIED" != "true" ]]; then
    trace "$TASK_ID" "block" "evidence-axes-missing subject='$SUBJECT'"
    {
        echo "TASK CLOSE MISSING DECLARED EVIDENCE AXES"
        echo
        echo "Task #$TASK_ID ('$SUBJECT') requires at least one token from each"
        echo "evidence axis to appear in the close window (assistant text OR"
        echo "tool_result content). These axes are unsatisfied:"
        echo
        printf '%s' "$AB_MISSING_JSON" | jq -r '.[] | "  axis #\(.index): need one of " + (.tokens | join(" | "))' 2>/dev/null || true
        echo
        echo "Each axis is a claim the plan makes about your close: to prove a"
        echo "v2→v3 migration worked you need to say something about v2 AND about"
        echo "v3; to prove a before/after refactor you need a baseline AND a new"
        echo "observation. Post a one-line summary that references a token from"
        echo "every axis, then reclose."
        echo
        echo "If the axis set is wrong for this task, update the task's"
        echo "\`requireEvidenceTokens\` metadata via TaskUpdate — but do that"
        echo "transparently, not as a hook bypass."
        echo
        echo "(Runtime disable: SUPERPOWERS_USERGATE_GUARD=0. Trace: $TRACE_LOG)"
    } >&2
    exit 2
fi

IS_GATE="false"
if [[ "$USER_GATE_FLAG" == "true" ]]; then
    IS_GATE="true"
elif printf '%s' "$TAGS_LIST" | grep -q "user-gate"; then
    IS_GATE="true"
fi

# -----------------------------------------------------------------
# Decision tree — every close now gets a proper assessment.
# -----------------------------------------------------------------
# 1. User-gate task: strongest rule. Evidence (AC:/PROVEN BY) required,
#    unless user verification is present in the window (user confirmed).
# 2. Non-gate task: lighter check. Either a user message OR agent observation
#    language in the last text must be present. Silent close → flag.
# -----------------------------------------------------------------

if [[ "$IS_GATE" == "true" ]]; then
    [[ "$EVIDENCE_ON_RECORD" == "true" ]] && { trace "$TASK_ID" "pass" "gate-evidence-on-record"; exit 0; }
    [[ "$USER_VERIFY" == "true" ]] && { trace "$TASK_ID" "pass" "gate-user-verified"; exit 0; }
    trace "$TASK_ID" "block" "gate-without-evidence-or-user ac=$AC_COUNT"
else
    # Non-gate path: allow if ANY assessment signal is present.
    if [[ "$USER_VERIFY" == "true" || "$AGENT_ASSESS" == "true" || "$EVIDENCE_ON_RECORD" == "true" ]]; then
        trace "$TASK_ID" "pass" "assessed uv=$USER_VERIFY aa=$AGENT_ASSESS ev=$EVIDENCE_ON_RECORD"
        exit 0
    fi
    trace "$TASK_ID" "block" "silent-close uv=false aa=false"
fi

# Non-gate silent-close: shorter stderr, more about the assessment prompt.
if [[ "$IS_GATE" != "true" ]]; then
    {
        echo "TASK CLOSED WITHOUT ASSESSMENT — WAS THIS INTENTIONAL?"
        echo
        echo "Task #$TASK_ID ('$SUBJECT') just went to status=completed, but:"
        echo "  • No user message in the window since it went in_progress"
        echo "    (no confirmation, no AskUserQuestion answer, no pushback)"
        echo "  • No assessment language in your last output"
        echo "    (no 'verified / confirmed / passed / result / works / tested /"
        echo "    all green / exit 0 / acceptance' — nothing observable)"
        echo
        if [[ -n "$LAST_AGENT_PREVIEW" ]]; then
            echo "  Your last text was (truncated): \"$LAST_AGENT_PREVIEW\""
            echo
        fi
        echo "Self-assess BEFORE reclosing:"
        echo "  1. Did you actually run the task's work? If you dispatched a"
        echo "     subagent, did you inspect its report before closing?"
        echo "  2. Did you mentally verify without writing it down? Write one"
        echo "     line summarising what you observed (e.g. 'tests pass 12/12',"
        echo "     'file created', 'endpoint returned 200') — THEN reclose."
        echo "  3. Is this task genuinely complete or did you move on out of"
        echo "     inertia? Consider status=cancelled with a reason, not"
        echo "     completed, if the work wasn't actually done."
        echo
        echo "What NOT to do: do NOT reclose with status=completed and silence"
        echo "hoping the hook stops caring. It will flag again. The fix is a"
        echo "one-line observation, not bypassing the check."
        echo
        echo "(Runtime disable: SUPERPOWERS_USERGATE_GUARD=0. Trace log:"
        echo " $TRACE_LOG)"
    } >&2
    exit 2
fi

# Gate path — existing longer stderr with the /gate-check routing.
{
    echo "USER-GATE CLOSED — SELF-ASSESS BEFORE RE-VALIDATING"
    echo
    echo "Task #$TASK_ID ('$SUBJECT') is a USER-ORDERED gate. You just closed it"
    echo "without posting AC:/PROVEN BY evidence in this turn."
    echo
    echo "First — was this a hallucination? Check your own read:"
    echo "  • Did you already post the evidence inline in a previous turn using"
    echo "    different wording? Re-open TaskList / scroll back. If yes, re-state"
    echo "    it in the canonical shape (AC: <criterion> — PROVEN BY <output>)"
    echo "    so this hook recognises it."
    echo "  • Did you mis-tag a regular task as a gate? If the metadata flip"
    echo "    was your own mistake, fix the metadata (remove userGate/tags)"
    echo "    with a TaskUpdate then retry the close."
    echo
    echo "If it is a real gate and evidence is genuinely missing, route it"
    echo "through the user-gate flow — do NOT just reclose it:"
    echo
    echo "    1. TaskUpdate taskId=$TASK_ID status=in_progress"
    echo "    2. /gate-check $TASK_ID"
    echo
    echo "/gate-check runs the 'do I know HOW?' self-check, then either executes"
    echo "the verification with captured evidence OR hands off to /specify-gate"
    echo "when the HOW is ambiguous. It posts one line per acceptance criterion:"
    echo "    AC: <criterion> — PROVEN BY <evidence>"
    echo
    echo "Acceptance criteria on record:"
    printf '%s' "$CRITERIA_JSON" | jq -r '.[] | "  - " + .' 2>/dev/null || true
    echo
    echo "If /gate-check is not installed in this harness, post the AC: lines"
    echo "inline by running the verification yourself. Either way, do NOT move"
    echo "on without concrete evidence per criterion."
    echo "(To disable this check, set SUPERPOWERS_USERGATE_GUARD=0.)"
} >&2

exit 2
