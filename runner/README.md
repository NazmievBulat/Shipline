# The runner pool

A repository that uses Shipline can run its CI on its own machines. This folder holds what such a
machine runs:
- the keepers that register the runners, one fresh container a job (`keep-runner.sh`);
- the spare and the ordinary slots (`keep-spare.sh`, `ordinary_slots.py`);
- the lender of main's runners to pull request jobs while main has none (`keep-lending.sh`,
  `lend_runners.py`);
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
| `<label>-docker` | the Docker-capable runners |
| `runner.planner_label` | the planner's runner; `<label>-planner` when it names none |

`runner.labels`, the labels the merge gate may re-run failed jobs on, are these six; a list
named in `shipline.json` wins over them. Machines differ only by their runners' names:
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
they would take each other's registrations.

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
take a newer version, run it again from main.

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
| `<prefix>-runner-image.service`, `<prefix>-runner-image.timer` | the images built again, every week |
| `<prefix>-runner-firewall.service` | the firewall, a unit of root's |
| `<prefix>-hub-cache.service` | the Docker Hub cache, when the repository runs one |
| `<prefix>-hub-cache-watch.service`, `<prefix>-hub-cache-watch.timer` | the cache emptied above its limit, every 10 minutes |
