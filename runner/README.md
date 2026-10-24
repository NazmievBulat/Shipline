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
| the base label | `runner.label`, `<prefix>-home` when it names none |

The prefix is the repository's word, not a runner's: `example-app`, not `example-app-runner`,
which would name `example-app-runner-runner…`. `workflow-metrics` reads the CPU of the
`<prefix>-runner…` services and containers.

## The labels: the same on every machine

Every machine's runners carry the same labels, so GitHub gives a job to whichever machine has
one free. They come from the base label, `runner.label` (`render-units.sh --labels` prints
them):

| Label | Which runners carry it |
| --- | --- |
| `<label>` | every ordinary runner |
| `<label>-standby` | a spare, before it takes a slot |
| `<label>-first` | the runners lent to the merge train's first member |
| `<label>-main` | main's runners, on the main machine only |
| `<label>-docker` | the Docker-capable runners |

`runner.labels`, the labels the merge gate may re-run failed jobs on, are these five; a list
named in `shipline.json` wins over them. Machines differ only by their runners' names:
`<machine>-1`, `<machine>-docker-1`, `<machine>-main` and `<machine>-planner`.

## The images

`build-images.sh` builds the runner images. The install runs it, and
`<prefix>-runner-image.timer` runs it again every week: GitHub refuses runners that fall far
behind its own version. It runs in the repository's main checkout and builds from `origin/main`
as last fetched, taken into a folder of its own, so whatever branch the checkout holds, a fork's
pull request among them, never runs with the runner user's Docker rights.

`runner.image_build` is the repository's own command line that builds them, read from main's
`shipline.json` at each build and run in that folder with `RUNNER_PREFIX` set. Without it, the
pool builds the ordinary image from the Dockerfile in this folder, with
`docker build --pull -t <prefix>-runner`, and then, only on a machine with `/dev/kvm`, the
Docker-capable image from `vm/`.

## A machine's own settings: `~/.config/shipline/runner.sh`

Each machine keeps what is its own in `~/.config/shipline/runner.sh`, a Bash file outside any
repository; `SHIPLINE_RUNNER_SETTINGS` names another file. It is never committed anywhere:

```bash
# This machine's name, in its runners' names: lowercase letters, digits and hyphens.
machine=home
# How many ordinary runners this machine runs.
runner_count=2
```

How many runners a machine has is its own choice.

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

The image build runs in `/var/lib/<prefix>-runner/checkout`, a link to the repository's main
checkout that the install writes; the runner user must be able to read it.
