# Sourced by build-images.sh and hub-cache.sh, after settings.sh, pool-settings.sh and names.sh.
# Runs one of the repository's own commands, `runner.<key>` of its shipline.json, on main's files
# as last fetched (`origin/main` of its main checkout), taken into a folder of their own: a branch
# the checkout holds, a fork's pull request among them, never runs here with Docker's rights.
#
#   on_main <key> [arguments]
#
# The command line runs in that folder with the arguments after it, RUNNER_PREFIX set and
# RUNNER_FILES naming this folder of runner files, which the repository's images take
# `start-runner.sh` and `vm/` from. The main checkout is the one install.sh recorded
# (`checkout`), else RUNNER_CHECKOUT, else the current folder.
on_main() {
  key=$1
  shift
  case "$prefix" in
    '' | [!a-z]* | *[!a-z0-9-]* | *-)
      echo "The prefix must be lowercase letters, digits and hyphens, starting with a letter: not '$prefix'" >&2
      return 1
      ;;
  esac
  from=${RUNNER_CHECKOUT:-${checkout:-$PWD}}
  if ! git -C "$from" rev-parse --quiet --verify "refs/remotes/origin/main^{commit}" >/dev/null; then
    echo "$from has no origin/main: run this from the repository's main checkout" >&2
    return 1
  fi
  files=$HOME/.local/state/$prefix-runner/main/$key
  rm -rf "$files"
  mkdir -p "$files"
  git -C "$from" archive --format=tar refs/remotes/origin/main | tar -x -C "$files"
  command=
  if [ -f "$files/shipline.json" ]; then
    command=$(jq -r --arg key "$key" '.runner[$key] // empty' "$files/shipline.json")
  fi
  if [ -z "$command" ]; then
    echo "main's shipline.json names no runner.$key (runner/README.md)" >&2
    return 1
  fi
  (
    cd "$files"
    RUNNER_PREFIX=$prefix RUNNER_FILES=$here bash -c "$command \"\$@\"" "runner.$key" "$@"
  )
}
