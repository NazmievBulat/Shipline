#!/bin/sh
# Only the host may change runner labels. Every ordinary container starts with the standby label;
# at most ordinary_jobs registrations (including busy and offline ones) get the workflow label.
# When an ephemeral registration disappears, an already registered spare takes its slot, while
# keep-runner.sh registers the replacement. No job receives a host folder, socket or credential.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/settings.sh"

# As the runner programs' own GitHub identity when the machine has one (github_identity.py).
gh() { python3 "$here/github_budget.py" call --identity runner keep-spare "$@"; }

# A second supervisor must not promote from the same snapshot. This file is not the settings.sh
# lock used by the Docker isolation check.
exec 9>"$here/spare.lock"
flock -n 9 || exit 1
listing=$(mktemp)
trap 'rm -f "$listing"' EXIT
trap 'exit 130' INT TERM

while true; do
  # The runner balancer (balance_runners.py) changes the counts while this runs: they are read
  # again each round, from a file it replaces whole.
  if [ -f "$here/pool-settings.sh" ]; then
    . "$here/pool-settings.sh"
  fi
  . "$here/names.sh"
  python3 "$here/github_budget.py" wait --identity runner keep-spare
  slots=$(python3 "$here/ordinary_slots.py" "$ordinary_jobs" "$ordinary_blacksmith_jobs" "$repository" \
    "$label")
  # Count every registration with the workflow label, even one we did not create, but not those of
  # another machine (names.sh): each machine has slots of its own. Nor one of main's runners lent
  # to PR jobs (lend_runners.py): that is no PR slot. An offline runner may return; a busy runner
  # still owns its slot until GitHub removes it. On an API error use no partial snapshot and grant
  # nothing. Pagination matters on repositories with >30 runners.
  if gh api --paginate "repos/$repository/actions/runners?per_page=100" --jq \
    '.runners[] | [.id, .name, .status, .busy, ([.labels[].name] | join(","))] | @tsv' >"$listing"; then
    # The planner of this machine counts the other machines' free runners from this listing
    # (planner-state.py). Replaced whole, so it never reads half of one.
    { cp "$listing" "$here/runners.tsv.new" && mv "$here/runners.tsv.new" "$here/runners.tsv"; } ||
      true
    occupied=$(awk -F '\t' -v label="$label" -v main="$main_label" -v first="$first_label" \
      -v total="$((ordinary_jobs + ordinary_spares))" \
      -v own="$own_runner" -v at="$own_number_at" -v others="$others" '
      ((index("," $5 ",", "," label ",") || index("," $5 ",", "," first ",")) &&
        !index("," $5 ",", "," main ",") && $2 !~ others) ||
      ($2 ~ own && substr($2, at)+0 >= 1 && substr($2, at)+0 <= total && $4 == "true") {n++}
      END {print n+0}' "$listing")
    available=$((slots - occupied))
    # A runner that lost the workflow label, or never had it, keeps no first label: it would still
    # take the jobs of the train's first member, beyond the slots.
    stray=$(awk -F '\t' -v label="$label" -v first="$first_label" \
      -v total="$((ordinary_jobs + ordinary_spares))" -v own="$own_runner" -v at="$own_number_at" '
      $2 ~ own && substr($2, at)+0 >= 1 && substr($2, at)+0 <= total &&
      $3 == "online" && $4 == "false" &&
      index("," $5 ",", "," first ",") && !index("," $5 ",", "," label ",") {print $1}' "$listing")
    for id in $stray; do
      gh api -X DELETE "repos/$repository/actions/runners/$id/labels/$first_label" >/dev/null ||
        true
    done
    if [ "$available" -lt 0 ]; then
      # Let busy jobs finish. Only our idle online runners lose the workflow label,
      # with other labels left alone. An ambiguous write waits for a new snapshot.
      retiring=$(awk -F '\t' -v label="$label" -v total="$((ordinary_jobs + ordinary_spares))" \
        -v own="$own_runner" -v at="$own_number_at" '
        $2 ~ own && substr($2, at)+0 >= 1 && substr($2, at)+0 <= total &&
        $3 == "online" && $4 == "false" && index("," $5 ",", "," label ",") {ids[NR]=$1}
        END {for (n=NR; n>0; n--) if (n in ids) print ids[n]}' "$listing")
      for id in $retiring; do
        [ "$available" -lt 0 ] || break
        gh api -X DELETE "repos/$repository/actions/runners/$id/labels/$label" >/dev/null || break
        available=$((available + 1))
        # Without the workflow label the first label alone would still take the train's jobs.
        gh api -X DELETE "repos/$repository/actions/runners/$id/labels/$first_label" >/dev/null ||
          true
      done
      # A successful demotion leaves no new slot to grant from this snapshot.
      sleep "$(python3 "$here/github_budget.py" jitter "$spare_poll_seconds")"
      continue
    fi
    # Only our online, idle, numbered ordinary runners are candidates. Docker runners never
    # enter this pool, and an offline spare cannot help a queued job.
    candidates=$(awk -F '\t' -v standby="$standby_label" -v label="$label" \
      -v total="$((ordinary_jobs + ordinary_spares))" -v own="$own_runner" -v at="$own_number_at" '
      $2 ~ own && substr($2, at)+0 >= 1 && substr($2, at)+0 <= total &&
      $3 == "online" && $4 == "false" &&
      index("," $5 ",", "," standby ",") && !index("," $5 ",", "," label ",") {print $1}' "$listing")
    for id in $candidates; do
      [ "$available" -gt 0 ] || break
      # A failed response can mean the write succeeded but the reply was lost. Do not grant
      # another slot from this snapshot; the next read must reconcile the actual labels first.
      # The first label goes with it: the jobs of the train's first member may take any PR slot.
      gh api -X POST "repos/$repository/actions/runners/$id/labels" \
        -f "labels[]=$label" -f "labels[]=$first_label" >/dev/null || break
      available=$((available - 1))
    done
  fi
  sleep "$(python3 "$here/github_budget.py" jitter "$spare_poll_seconds")"
done
