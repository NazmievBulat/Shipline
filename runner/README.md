# The runner pool

A repository that uses Shipline can run its CI on its own machines. This folder holds what a
machine needs for that: the programs that register the runners, keep the ordinary slots and a
spare, lend main's runners to pull request jobs while main has none, plan, and sample usage;
the installers; and the systemd units, as templates. Shipline's own CI runs on GitHub-hosted
runners only: Shipline never registers a runner to Shipline itself.

Nothing here names a repository, a machine, a user or a host. One prefix names everything, and
what differs between machines stays on each machine, outside any repository.

## The prefix: one word in `shipline.json`

The repository names its prefix as `runner.prefix` in its `shipline.json`:

```json
{
  "runner": {
    "prefix": "example-app",
    "image_build": "tools/shipline/build-runner-images"
  }
}
```

It is at most 25 lowercase letters, digits and hyphens, starting with a letter and ending with
a letter or digit, because it names systemd units and a user. Every name comes from it
(`render-units.sh --names` prints them):

| What | Name |
| --- | --- |
| the user the runners run as | `<prefix>-runner` |
| the installed programs | `/opt/<prefix>-runner` |
| the runners' state | `/var/lib/<prefix>-runner` |
| the ordinary image | `<prefix>-runner` |
| the Docker-capable image | `<prefix>-runner-docker` |
| the container network | `<prefix>-runner` |
| the Docker Hub cache | `<prefix>-runner-hub-cache` |
| the systemd units | `<prefix>-runner…` |
| a machine's labels | `<prefix>-<machine>`, `<prefix>-<machine>-main`, `<prefix>-<machine>-docker` |
| the planner's label | `<prefix>-planner` |

`runner.labels` is built from the prefix and the machines (below): the labels the merge gate
may re-run failed jobs on. A list named in `shipline.json` wins over the built one.
`workflow-metrics` reads the CPU of the `<prefix>-runner…` services and containers.

`runner.image_build` is the repository's own command line that builds the runner images, run
from its main checkout with `RUNNER_PREFIX` set. Without it, `build-images.sh` builds the
ordinary image from the Dockerfile in this folder, with `docker build --pull -t <prefix>-runner`,
and then, only on a machine with `/dev/kvm`, the Docker-capable image from `vm/`. The install
builds them, and `<prefix>-runner-image.timer` builds them again every week: GitHub refuses
runners that fall far behind its own version.

## A machine's own settings: `~/.config/shipline/runner.sh`

Each machine keeps what is its own in `~/.config/shipline/runner.sh`, a Bash file outside any
repository; `SHIPLINE_RUNNER_SETTINGS` names another file. It is never committed anywhere:

```bash
# This machine's name, in its labels: lowercase letters, digits and hyphens.
machine=home
# The other machines of the pool, whose runners the merge gate may also re-run jobs on.
other_machines=(office)
# How many runners this machine runs.
runner_count=2
```

The scripts read it in a `bash` of its own, with only `PATH` and `HOME`, for at most 5 seconds;
a file that fails, never ends or names something that is not a machine name is refused by its
path. Without the file, the only label built is the planner's. How many runners a machine has
is its own choice.

## The units

`render-units.sh <folder>` writes the units from the templates in `units/`, with `@PREFIX@`
replaced by `RUNNER_PREFIX`; nothing else in them varies, and `tests/runner/` checks them for
the prefix `example-app` against `tests/runner/expected/`.

| Unit | What it does |
| --- | --- |
| `<prefix>-runner@.service` | one ordinary runner, slot `%i`, in a container of `<prefix>-runner` |
| `<prefix>-runner-main@.service` | one of main's runners, slot `%i`, which the lender lends while main has no job |
| `<prefix>-runner-docker@.service` | one Docker-capable runner, slot `%i`, in a virtual machine; only with `/dev/kvm` |
| `<prefix>-runner-planner.service` | the planner's runner, for the jobs that plan what CI runs |
| `<prefix>-runner-spare.service` | the spare supervisor, which keeps a registered spare runner ready |
| `<prefix>-runner-lend.service` | the lender |
| `<prefix>-runner-usage.service`, `<prefix>-runner-usage.timer` | a sample of what the runners use, every minute |
| `<prefix>-runner-image.service`, `<prefix>-runner-image.timer` | the images built again, every week |
| `<prefix>-runner-network.service` | the container network `<prefix>-runner` |
| `<prefix>-runner-hub-cache.service` | a pull-through cache of Docker Hub on that network |

The image build runs from `/var/lib/<prefix>-runner/checkout`, a link to the repository's main
checkout, with the values of `images.env` beside it (`RUNNER_PREFIX`, and
`SHIPLINE_RUNNER_IMAGE_BUILD` when the repository names one); the install writes both.

## The balancer

`balance_runners.py` moves a machine's runners between CI and Docker, and adds or removes them,
by the queues, the machine's load and the demand on its way. Its user timer,
`<prefix>-runner-balance.timer`, runs it every minute beside the spare supervisor; `install.sh`
starts it on a machine with the `ordinary` or `docker` role. It changes only that machine's own
ordinary slots (`<prefix>-runner@<k>.service`) and Docker runners
(`<prefix>-runner-docker@<k>.service`), never another machine's, never a main runner or the
planner.

### The bounds

It changes a kind only within the bounds in the machine's `pool-settings.sh`, beside the
installed scripts:

    balance_ordinary=2-6
    balance_docker=0-2

A kind without bounds, or one the machine has no role for, keeps its installed count
(`ordinary_jobs`, `docker_runners`) as both bounds, so the balancer changes nothing there until
its owner widens them. A line `balance off` stops it, and the counts stay as they are. When it
adds or removes a runner, it writes the new count to that line, and the spare supervisor reads it
at its next round. A reinstall keeps the bounds, `balance off` and `balance_forecast`. The counts
are then the ones the install was given.

An ordinary slot is one of the `ordinary_jobs` registrations that carry the pool's label. Adding
one starts the next unit after the slots and the spare. A Docker runner needs `/dev/kvm`. All
the runners together never take more than `most_runners` places.

### What it does

Each minute it reads, from GitHub under the runners' own identity and within the shared budget:

- the queued and running jobs for the pool's labels (`label` and `docker_label`), which every
  machine's runners share;
- the pool's runners.

It also reads the machine's 5-minute load against the session start ceiling (the processors
times 1.5) and its available memory.

- **Add:** an ordinary slot when ordinary jobs have queued for 5 minutes, or a Docker runner
  when Docker jobs have, when there is room: load at least 4 under the ceiling, and at least
  3 GB available for an ordinary slot or 8 GB for a Docker runner.
- **Remove:** one of a kind that had nothing queued for 15 minutes while the load holds session
  starts.
- **Move:** when the other kind has queued for 5 minutes, this kind has nothing queued and
  there is no room to add, it drains one of this kind and adds one of the other in the same
  step.
- A runner stops only once GitHub lists it idle, never mid-job. Its registration is deleted
  before its unit stops, and GitHub refuses that while the runner runs a job. The
  highest-numbered one goes (`<machine>-<k>`, `<machine>-docker-<k>`), and the step waits while
  it is busy.
- At most one change every 5 minutes.

### The forecast

Each turn the tick writes `~/.local/state/workflow/ci-demand.json`: about how many ordinary and
Docker jobs the pushes on their way will start within 30 minutes. Those pushes come from the
working cloud workers, the busy local workers and reviewers, the ready pull requests whose
checks have not started, and the merge line's prefixes still to be built. Each push counts the
jobs of its pull request's last runs. A machine's share of a kind is the forecast times its own
slots of that kind over all the pool's online slots of that kind.

- A removal waits while the machine's share would queue on one slot fewer.
- A slot comes back before the wave when the share would queue on the slots there are and there
  is room, never beyond the bounds.
- A forecast older than 10 minutes is ignored, and the queues alone decide.

The balancer on the tick's machine reads that file directly. Another machine reads the path its
`pool-settings.sh` names with `balance_forecast=`, a copy its owner provides. Without one, it
balances by the queues alone.

### The log

Every change is one line of `~/.local/state/workflow/runner-balance.log`: the time, what changed,
the queues, the forecast, the load and the memory. On the tick's machine, the wake's next
check-in names the last change once.
