#!/usr/bin/env bash
# variant=local
# PreToolUse hook: block git commit while a native task is in progress.
# Local refit of superpowers-extended-cc marketplace original — delegates
# task-state extraction to tools/local_hooks/lib/tasks_live_query.py
# which reads the Claude Code harness's live on-disk task store at
# ~/.claude/tasks/<sessionId>/<taskId>.json instead of the lagging
# session JSONL transcript. See:
#   docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md

INPUT=$(cat)
ALLOW='{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}'
HELPER_ROOT="${KINOFORGE_LOCAL_HOOKS_DIR:-$HOME/.claude/hooks}"

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
[[ "$TOOL_NAME" != "Bash" ]] && echo "$ALLOW" && exit 0

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
echo "$COMMAND" | grep -qE '(^|[;&|(]|&&|\|\|)[[:space:]]*git[[:space:]]+commit([[:space:]]|[;&|)]|$)' || { echo "$ALLOW"; exit 0; }

TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
[[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]] && echo "$ALLOW" && exit 0

SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
RESULT=$(python3 "$HELPER_ROOT/lib/tasks_live_query.py" \
    --session-id "$SESSION_ID" \
    --transcript "$TRANSCRIPT_PATH" 2>/dev/null)

if [[ -z "$RESULT" ]]; then
    echo "$ALLOW"; exit 0
fi

OPEN_TASKS=$(echo "$RESULT" | jq -r '.in_progress_count // 0')

if [[ "$OPEN_TASKS" -gt 0 ]]; then
    SOURCE=$(echo "$RESULT" | jq -r '.source // "?"')
    echo "COMMIT BLOCKED: $OPEN_TASKS native task(s) still in progress (source=$SOURCE, variant=local). Finish the current task before committing." >&2
    exit 2
fi

echo "$ALLOW"
exit 0
