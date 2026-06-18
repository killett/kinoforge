#!/usr/bin/env bash
# Install the live-task-store reader + 5 refit hooks into ~/.claude/hooks/
# and swap ~/.claude/settings.json to use the local copies.
#
# Usage:
#   bash tools/local_hooks/install.sh            # live install
#   bash tools/local_hooks/install.sh --dry-run  # print intended actions only
#
# Idempotent: re-running on a system that's already installed copies
# the (possibly updated) sources but does NOT make a second backup
# (the first one stays as the original-marketplace reference).
#
# Uninstall: cp $HOME/.claude/settings.json.pre-hook-swap-2026-06-18.bak \
#                 $HOME/.claude/settings.json

set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"
BACKUP="$HOME/.claude/settings.json.pre-hook-swap-2026-06-18.bak"

cleanup_on_failure() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "" >&2
        echo "Install failed (exit $rc). Partial state may exist:" >&2
        echo "  - Files copied to $DST may persist" >&2
        echo "  - settings.json modification may have failed" >&2
        echo "  - Backup at $BACKUP is intact; restore with:" >&2
        echo "      cp \"$BACKUP\" \"$SETTINGS\"" >&2
    fi
}
trap cleanup_on_failure EXIT

HOOKS=(
    pre-commit-check-tasks.sh
    pre-task-blockedby-enforce.sh
    post-task-complete-revalidate.sh
    post-agent-return-validate.sh
    stop-revalidate-user-gates.sh
)

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "DRY-RUN: $*"
    else
        eval "$@"
    fi
}

echo "Source dir : $SRC"
echo "Dest dir   : $DST"
echo "Settings   : $SETTINGS"
echo "Backup path: $BACKUP"
echo

# 1. Create destination dirs.
run "mkdir -p $DST/lib"

# 2. Copy helper + README.
run "cp $SRC/lib/tasks_live_query.py $DST/lib/tasks_live_query.py"
run "cp $SRC/README.md $DST/lib/README.md"

# 3. Copy + chmod each hook.
for hook in "${HOOKS[@]}"; do
    run "cp $SRC/$hook $DST/$hook"
    run "chmod +x $DST/$hook"
done

# 4. Back up settings.json (only if backup doesn't already exist).
if [[ ! -f "$BACKUP" ]]; then
    run "cp $SETTINGS $BACKUP"
else
    echo "Backup already exists at $BACKUP — preserving original."
fi

# 5. Rewrite settings.json paths.
# Use python for safe JSON edit instead of sed.
PY_REWRITE=$(cat <<'PY'
import json, os, sys
from pathlib import Path

settings_path = Path(os.environ["SETTINGS"])
home = os.environ["HOME"]
hooks = os.environ["HOOKS"].split()
data = json.loads(settings_path.read_text())

marketplace_prefix = f"{home}/.claude/plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples"

def walk(node):
    if isinstance(node, dict):
        cmd = node.get("command")
        if isinstance(cmd, str):
            for hook in hooks:
                old = f"bash {marketplace_prefix}/{hook}"
                new = f"bash {home}/.claude/hooks/{hook}"
                if cmd == old:
                    node["command"] = new
                    return
        for v in node.values():
            walk(v)
    elif isinstance(node, list):
        for v in node:
            walk(v)

walk(data)
settings_path.write_text(json.dumps(data, indent=2) + "\n")
PY
)

if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY-RUN: would rewrite hook command paths in $SETTINGS"
else
    SETTINGS="$SETTINGS" HOOKS="${HOOKS[*]}" python3 -c "$PY_REWRITE"
fi

echo
echo "Install complete. Verify with:"
echo "  python3 $DST/lib/tasks_live_query.py --help"
echo "  grep -c '\\$HOME/.claude/hooks' $SETTINGS"
