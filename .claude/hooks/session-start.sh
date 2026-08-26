#!/bin/bash
# Claude Code SessionStart hook.
#
# Installs the Python dependencies the feed-curator scripts need so a scheduled
# wake can run fetch_state.py / build_feed.py / push_state.py without dying on a
# missing import. Runs on every session start in Claude Code on the web (the
# environment the scheduled routine wakes in).
set -euo pipefail

# Only run in the remote (web) environment. Local sessions manage their own
# virtualenv, so we don't touch a developer's machine. Remove this guard if you
# want deps installed on local sessions too.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Install deps. This is the non-deterministic step (hits PyPI, resolves the
# latest matching wheels), which is exactly why it lives in the per-session
# startup hook rather than a baked image. All requirements are pure-Python
# wheels, so there is no compile step to fail on.
#
# Repo root: the harness sets CLAUDE_PROJECT_DIR; fall back to this script's
# location (.claude/hooks/ -> repo root) so the hook also works when run by hand.
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# pip's progress is sent to stderr so this hook's stdout — which SessionStart
# adds to the session context — stays clean. Idempotent: safe to re-run.
python -m pip install --disable-pip-version-check \
  -r "$PROJECT_DIR/requirements.txt" 1>&2

echo "session-start: python dependencies ready"
