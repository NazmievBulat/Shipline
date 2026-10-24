# Sourced by build-images.sh and hub-cache.sh, after settings.sh, pool-settings.sh and names.sh.
# Runs one of the repository's own commands, `runner.<key>` of its shipline.json, on main's files
# as last fetched (`origin/main` of its main checkout), taken into a folder of their own: a branch
# the checkout holds, a fork's pull request among them, never runs here with Docker's rights.
#
#   on_main <key> [arguments]
#
# The command line runs in that folder with the arguments after it, RUNNER_PREFIX set and
# RUNNER_FILES naming this folder of runner files, which the repository's images take
# `start-runner.sh` and `vm/` from. Each key, and each first argument (`hub-cache.sh watch`), has
# a folder of its own, taken again only when main has moved: the watch never takes away the
# files of the cache that runs. The main checkout is RUNNER_CHECKOUT (install.sh names it before
# it records it), else the one install.sh recorded (`checkout`), else the current folder.
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
  case "${1:-}" in
    *[!a-z0-9-]*)
      echo "'$1' is no word a folder may be named by" >&2
      return 1
      ;;
  esac
  files=$HOME/.local/state/$prefix-runner/main/$key${1:+-$1}
  mkdir -p "${files%/*}"
  # One run of a folder at a time: a second one (the install's build while the timer's runs) waits,
  # so it never takes away the files of the run under way. The command itself does not hold the
  # lock's descriptor, so nothing it leaves running keeps the lock; it is not the subshell's last
  # command either, which sh would run in the subshell's place and so let the lock go.
  (
    flock 9
    commit=$(git -C "$from" rev-parse refs/remotes/origin/main)
    if [ "$(cat "$files.commit" 2>/dev/null)" != "$commit" ]; then
      rm -rf "$files" "$files.commit"
      mkdir -p "$files"
      git -C "$from" archive --format=tar "$commit" | tar -x -C "$files"
      printf '%s\n' "$commit" >"$files.commit"
    fi
    command=
    if [ -f "$files/shipline.json" ]; then
      command=$(jq -r --arg key "$key" '.runner[$key] // empty' "$files/shipline.json")
    fi
    if [ -z "$command" ]; then
      echo "main's shipline.json names no runner.$key (runner/README.md)" >&2
      exit 1
    fi
    cd "$files"
    RUNNER_PREFIX=$prefix RUNNER_FILES=$here bash -c "$command \"\$@\"" "runner.$key" "$@" 9>&- ||
      exit
  ) 9>"$files.lock"
}
