#!/bin/sh
# Install the same call limiter for interactive sessions and installed host services.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
# Named by the pool's prefix, which install.sh gives it.
installed=$HOME/.local/share/${prefix:?install-github-budget.sh needs the prefix}-github
link=$HOME/.local/bin/gh
if { [ -e "$link" ] || [ -L "$link" ]; } &&
   [ "$(readlink -f "$link")" != "$installed/github-gh.sh" ]; then
  echo "GitHub budget wrapper: $link already belongs to another installation; left unchanged." >&2
  exit 1
fi
upstream=$(WORKFLOW_GITHUB_RAW_GH="${WORKFLOW_GH:-${WORKFLOW_GITHUB_RAW_GH:-}}" \
  python3 "$here/github_budget.py" raw)
case "$upstream" in
  /*) ;;
  *) upstream=$(command -v "$upstream") ;;
esac
case "$(readlink -f "$upstream")" in
  "$installed/github-gh.sh") upstream=$(cat "$installed/github-upstream") ;;
esac
mkdir -p "$installed" "${link%/*}"
cp -L "$here/github_budget.py" "$here/github-gh.sh" "$installed/"
printf '%s\n' "$upstream" >"$installed/github-upstream"
chmod 755 "$installed/github-gh.sh"
ln -sfn "$installed/github-gh.sh" "$link"
