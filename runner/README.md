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
