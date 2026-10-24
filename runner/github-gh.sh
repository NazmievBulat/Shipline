#!/bin/sh
# Stable entry point for sessions and other host programs. Local/interactive auth
# commands keep their terminal; non-interactive GitHub work shares the host limiter.
set -eu
here=$(dirname "$(readlink -f "$0")")
case "${1:-}" in
  auth|config|completion|help|version|--version|--help)
    upstream=${WORKFLOW_GITHUB_RAW_GH:-$(cat "$here/github-upstream" 2>/dev/null || echo /usr/bin/gh)}
    exec "$upstream" "$@"
    ;;
esac
exec python3 "$here/github_budget.py" foreground "$@"
