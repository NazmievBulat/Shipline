# The runner pool

A repository that uses Shipline can run its CI on its own machines. This folder holds what such a
machine runs:
- the keepers that register the runners, one fresh container a job (`keep-runner.sh`);
- the spare and the ordinary slots (`keep-spare.sh`, `ordinary_slots.py`);
- the lender of main's runners to pull request jobs while neither main nor the merge line has a
  job for them, which also gives them the merge line's lane while no push to main waits
  (`keep-lending.sh`, `lend_runners.py`);
- the planner's runner and its state (`keep-planner.sh`, `planner-state.py`);
- the usage sampler (`sample-usage.sh`, `usage.py`);
- the firewall that shuts the jobs in (`firewall.sh`, `install-firewall.sh`);
- the Docker-capable runner's virtual machine (`vm/`) and its check (`check-docker-runner.sh`);
- the installer, `install.sh`;
- the systemd units, as templates (`systemd/`).

Shipline's own CI runs on GitHub-hosted runners only: Shipline never registers a runner to
Shipline itself.

Nothing here names a repository, a machine or a host. The repository names the pool in its
`shipline.json`; what differs between machines stays on each machine, outside any repository.

## The repository's part: `shipline.json`

```json
{
  "repository": "example-owner/example-app",
  "runner": {
    "prefix": "example-app",
    "label": "example-app-home",
    "planner_label": "example-app-home-planner",
    "image_build": "tools/shipline/build-runner-images",
    "hub_cache": "tools/shipline/hub-cache"
  }
}
```

`runner.prefix` is one word: at most 25 lowercase letters, digits and hyphens, starting with a
letter and ending with a letter or digit. It is the repository's word, not a runner's:
`example-app`, not `example-app-runner`, which would name `example-app-runner-runner…`. Every
name comes from it (`names.sh`; `render-units.sh --names` prints them):

| What | Name |
| --- | --- |
| the installed scripts | `~/.local/share/<prefix>-runner` |
| the firewall's scripts, root's | `/usr/local/lib/<prefix>-runner` |
| the ordinary image, and the Docker-capable one | `<prefix>-runner`, `<prefix>-runner-docker` |
| the runners' networks, one for each place | `<prefix>-runner-<place>` |
| the runners' containers | `<prefix>-runner-<slot>`, `<prefix>-runner-main`, `<prefix>-runner-docker-<slot>`, `<prefix>-runner-planner` |
| the Docker Hub cache, its container and network | `<prefix>-hub-cache` |
| the systemd units | `<prefix>-runner…`, `<prefix>-hub-cache…` |

`workflow-metrics` reads the CPU of the `<prefix>-runner…` services and containers.

### The labels: the same on every machine

Every machine's runners carry the same labels, so GitHub gives a job to whichever machine has
one free. They come from the base label, `runner.label`, `<prefix>-home` when it names none
(`render-units.sh --labels` prints them):

| Label | Which runners carry it |
| --- | --- |
| `<label>` | the ordinary runners that hold a PR slot |
| `<label>-standby` | an ordinary runner before it takes a slot, the spare among them |
| `<label>-first` | the slots, and the main runner lent to the merge train's first member |
| `<label>-main` | main's runners, on the main machine only |
| `<label>-main-batch` | main's idle runners while no push to main waits: the lane the merge gate's first run of a prefix takes when its planner finds one free (`examples/ci.yml`) |
| `<label>-docker` | the Docker-capable runners |
| `runner.planner_label` | the planner's runner; `<label>-planner` when it names none |

`runner.labels`, the labels the merge gate may re-run failed jobs on, are these seven; a list
named in `shipline.json` wins over them, and then names `<label>-main-batch` too. The merge
line's lane is taken only by a planner that runs on the planner's runner, which reads the
lender's note (`ci_proof.py main-lane`). Machines differ only by their runners' names:
`<machine>-1`, `<machine>-docker-1`, `<machine>-main` and `<machine>-planner`.

### The repository's own commands

The images and the Docker Hub cache are the repository's. Its own command lines build and run
them. They run on `origin/main` as last fetched, taken into a folder of their own (`main-command.sh`),
so a branch the checkout holds, a fork's pull request among them, never runs with Docker's
rights. Each gets `RUNNER_PREFIX`, and `RUNNER_FILES`: the installed copy of this folder,
where its images take `start-runner.sh` and `vm/` from.

- `runner.image_build` builds `<prefix>-runner`, and on a machine with `/dev/kvm`
  `<prefix>-runner-docker` (`build-images.sh`). The install runs it, and
  `<prefix>-runner-image.timer` runs it again every week: GitHub refuses runners that fall far
  behind its own version. The virtual machine's image keeps what its work folder held when it
  was built, `list-work /home/runner/_work`, in
  `/usr/local/share/shipline-runner/work-as-shipped`, which `check-isolation` compares a job's
  start with. Without the command the pool cannot be installed.
- `runner.hub_cache` runs the Docker Hub cache of the virtual machines (`hub-cache.sh`), with
  `watch` after it every 10 minutes to empty the cache above its limit. It gets where the cache
  must be: `HUB_CACHE_CONTAINER` on `HUB_CACHE_NETWORK` (`HUB_CACHE_SUBNET`), at
  `HUB_CACHE_ADDRESS` and `HUB_CACHE_PORT`, and `HUB_CACHE_MOST_MEGABYTES`. Without the command,
  no cache is installed, and the virtual machines pull from Docker Hub.

## A machine's own settings: `~/.config/shipline/runner.sh`

Each machine keeps what is its own in `~/.config/shipline/runner.sh`, a file outside any
repository that `install.sh` reads; `SHIPLINE_RUNNER_SETTINGS` names another file:

```sh
# This machine's name, which starts its runners' names: one word of small letters and digits.
machine=home
# The other machines of the pool. Their runners are theirs to retire, label or count.
other_machines=office
# What this machine runs: main (main's runners and the lender), ordinary (PR slots and the spare),
# docker (Docker-capable runners, with /dev/kvm) and planner (the CI planner's runner).
roles='main ordinary planner'
# How many PR job slots and Docker-capable runners it gets when install.sh is given no count.
ordinary_jobs=2
docker_runners=0
# The PR slots it keeps while hosted overflow has budget.
ordinary_blacksmith_jobs=1
```

How many runners a machine has is its own choice. Two machines must never have the same name:
they would take each other's registrations. A machine runs one pool: the addresses of the
runners' networks and of the cache (`settings.sh`), the runners' GitHub identity (in the folder
`runner.identity_folder` names, `~/.config/shipline-runner` by default) and the planner's notes
are the machine's, not a prefix's.

## Installing a machine

In the repository's main checkout, on main:

```sh
sh <shipline>/runner/install.sh [--github-token <file> | --github-app <app id> <key file>]
sudo sh ~/.local/share/<prefix>-runner/install-firewall.sh
```

`install.sh` copies this folder out of the checkout, renders the units for the prefix
(`render-units.sh`), builds the images, and starts the runners as services of your user. It
records the repository's names, its checkout and the machine's settings in `pool-settings.sh`
beside the copy. The runners wait until `install-firewall.sh` has put the firewall in place. To
take a newer version, run it again from main. To change the prefix, stop the pool's services and
remove `~/.local/share/<prefix>-runner` first: an install under a new prefix does not find the
old one.

## The units

`render-units.sh <folder>` writes the units from the templates in `systemd/`, with `@PREFIX@`
replaced by `RUNNER_PREFIX`. Nothing else in them varies, and `tests/runner/` checks them for
the prefix `example-app` against `tests/runner/expected/`.

| Unit | What it does |
| --- | --- |
| `<prefix>-runner@.service` | one ordinary runner, slot `%i` |
| `<prefix>-runner-main.service`, `<prefix>-runner-main-2.service` | main's two runners |
| `<prefix>-runner-docker@.service` | one Docker-capable runner, slot `%i`, a fresh virtual machine for each job |
| `<prefix>-runner-planner.service` | the planner's runner |
| `<prefix>-runner-spare.service` | the spare supervisor |
| `<prefix>-runner-lend.service` | the lender |
| `<prefix>-runner-usage.service`, `<prefix>-runner-usage.timer` | a line of what the machine has free, every minute |
| `<prefix>-runner-balance.service`, `<prefix>-runner-balance.timer` | the balancer (below), every minute |
| `<prefix>-runner-image.service`, `<prefix>-runner-image.timer` | the images built again, every week |
| `<prefix>-runner-firewall.service` | the firewall, a unit of root's |
| `<prefix>-hub-cache.service` | the Docker Hub cache, when the repository runs one |
| `<prefix>-hub-cache-watch.service`, `<prefix>-hub-cache-watch.timer` | the cache emptied above its limit, every 10 minutes |

## Moving a machine's PR slots

A machine that also runs the sessions, the planner and main's runners can go over its limit while
another machine has room. Its ordinary PR slots may then move to the machine with room, but only
once measured use there shows that room on every day of a week. The machine table and its reasons
are written from the measured reports, never from guesses.

1. Both machines run this version for seven whole days; `<prefix>-runner-usage.timer` writes one
   line a minute of what each has free.
2. On the receiving machine, run
   `python3 ~/.local/share/<prefix>-runner/usage.py report --days 7 --job-memory <cap>`, with
   the cap of one of its ordinary jobs (its `job_memory`, else `memory` of `settings.sh`, `10g`).
   It prints each day's busiest hour, then `Room for <n> more PR jobs`: none unless every day had
   room, else the least memory available at a busiest hour, less the 8 GiB margin, in jobs of
   that cap. Only when it ends with `Room every day: yes` (exit 0) do slots move, and never more
   than that `<n>`; main's runners and the planner never move. When it says there is no room,
   nothing moves, and the report goes to the owner.
3. The move is a change of each machine's own settings (`~/.config/shipline/runner.sh`) and an
   install of each, by the machine's owner and on the owner's word, the receiving machine first:
   - The receiving machine raises its `ordinary_jobs` by the slots that move, no more than the
     report's `<n>`, and may name its own `job_memory` (`6g`).
   - Once its runners are online with the base label, the giving machine lowers its
     `ordinary_jobs` by as many and installs. `install.sh` refuses to lower it while GitHub
     lists no online ordinary runner of another machine with the base label: with the receiving
     machine down, unreachable or not installed, both counts stay as they are.
   - In a pool of several machines, `install.sh` reads GitHub's runners before it changes
     anything but the runners' GitHub identity, which it checks against GitHub before it takes
     it; an install whose list cannot be read is refused. A machine alone in its pool is not
     checked.

   Nothing names where the jobs go: every machine's runners carry the same labels, so GitHub
   gives a job to whichever runner is free.

   Moving slots back (step 6) is the same the other way round, and is bounded by no room: the
   slots return to the machine they came from, which raises its `ordinary_jobs` again and
   installs first; the other machine lowers its count once those runners are online.
4. A machine may keep no PR slot of its own (`ordinary_jobs=0`) while the pool has another
   machine; the only machine of a pool keeps at least one, and so does at least one machine of
   every pool: two machines with none would each take a slot and give it back as the other did.
   PR jobs still run when no other machine has an online ordinary runner: the giving machine's
   spare then takes one slot, and gives it back once another machine's ordinary runner is online
   again (`keep-spare.sh`, `ordinary_slots.py`). Main's runner lent to PR jobs does not count, as
   it is busy exactly when main has work.
5. The pull request that records the move states, measured before and after it: the queue wait of
   PR jobs (each job's `started_at` minus `created_at`: median, 90th percentile and longest), and
   each machine's CPU pressure at its busiest hour.
6. When timing-sensitive tests fail on the receiving machine while it is full, the slots go back.

## The balancer

`balance_runners.py` moves a machine's runners between CI and Docker, and adds or removes them,
by the queues, the machine's load and the demand on its way. Its user timer,
`<prefix>-runner-balance.timer`, runs it every minute beside the spare supervisor; `install.sh`
starts it on a machine with the `ordinary` or `docker` role. It changes only that machine's own
ordinary slots (`<prefix>-runner@<k>.service`) and Docker runners
(`<prefix>-runner-docker@<k>.service`), never another machine's, never a main runner or the
planner.

### The bounds

It changes a kind only within the bounds the machine's settings give it, in
`~/.config/shipline/runner.sh`:

```sh
balance_ordinary=2-6
balance_docker=0-2
```

`install.sh` carries them, and a line `balance off`, into the installed `pool-settings.sh`,
which is what the balancer reads. A reinstall keeps the installed ones when the machine's
settings name none.

- A kind without bounds, or one the machine has no role for, keeps its installed count
  (`ordinary_jobs`, `docker_runners`) as both bounds, so the balancer changes nothing there until
  its owner widens them.
- `balance off` stops it, and the counts stay as they are.
- When it adds or removes a runner, it writes the new count to `pool-settings.sh`, and the spare
  supervisor and the planner read it at their next round. A reinstall starts again from the
  counts of the machine's settings.

An ordinary slot is one of the `ordinary_jobs` registrations that carry the pool's label. Adding
one starts the next unit after the slots and the spare, and only once every slot there is has
the label. A Docker runner needs `/dev/kvm`. All the runners together never take more than
`most_runners` places of the firewall's range.

### What it does

Each minute it reads, from GitHub under the runners' own identity and within the shared budget:

- the queued and running jobs for the pool's labels (`label`, the first member's `first_label`
  and `docker_label`), which every machine's runners share;
- the pool's runners.

It also reads the machine's 5-minute load against the session start ceiling (the processors
times 1.5) and its available memory.

- **Add:** an ordinary slot when ordinary jobs have queued for 5 minutes, or a Docker runner
  when Docker jobs have, when there is room: load at least 4 under the ceiling, and at least
  3 GB available for an ordinary slot or 8 GB for a Docker runner.
- **Remove:** one of a kind that had nothing queued for 15 minutes while the load holds session
  starts.
- **The pool's last runner of a kind:** a machine stops or moves away its own last runner of a
  kind only while a machine whose name sorts before its own lists an online runner of that kind
  (main's runners and the spares not counted). The machine of the lowest name that has one never
  stops its last, so two machines balancing at once never leave the pool without one.
- **Move:** when the other kind has queued for 5 minutes, this kind has nothing queued and
  there is no room to add, it drains one of this kind and adds one of the other in the same
  step.
- A runner stops only once GitHub lists it idle, never mid-job. Its registration is deleted
  before its count is lowered and its unit stops, and GitHub refuses that while the runner runs a
  job. A paused budget or a call that fails leaves the counts as they are. The
  highest-numbered one goes (`<machine>-<k>`, `<machine>-docker-<k>`), and the step waits while
  it is busy.
- At most one change every 5 minutes. `install.sh` holds the balancer's lock while it
  reinstalls.

### The forecast

Each turn the tick writes `~/.local/state/workflow/ci-demand.json`: about how many ordinary and
Docker jobs the pushes on their way will start within 30 minutes. Those pushes come from the
working cloud workers, the busy local workers and reviewers, the workflow's ready pull requests
whose checks have not started, and the merge line's prefixes still to be built. Each push counts
the jobs its pull request's last runs started. A machine's share of a kind is the forecast times
its own slots of that kind over all the pool's online slots of that kind.

- A removal waits while the machine's share would queue on one slot fewer.
- A slot comes back before the wave when the share would queue on the slots there are and there
  is room, never beyond the bounds.
- A forecast older than 10 minutes is ignored, and the queues alone decide.

The balancer on the tick's machine reads that file directly. Another machine reads the path its
settings name with `balance_forecast=`, a copy its owner provides. Without one, it balances by
the queues alone.

### The log

Every change is one line of `~/.local/state/workflow/runner-balance.log`: the time, what changed,
the queues, the forecast, the load and the memory. On the tick's machine, the wake's next
check-in names the last change once.
