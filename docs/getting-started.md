# Getting started

Shipline runs on one Linux machine and works on one repository on GitHub. You open a ticket as
a draft pull request. A worker session builds it, a fresh reviewer session checks it, and the
merge gate squashes it into `main` once CI is green. A timer runs the tick every 3 minutes; the
tick starts the sessions and drives the merges.

This guide sets up a repository from nothing and takes one ticket through to `main`. A test
runs every command shown here (`tests/workflow/test_getting_started.py`).

## What Shipline needs

- Linux with systemd user units: `systemctl --user` must reach your service manager.
- `tmux`, `git` and `jq`.
- `gh`, signed in (`gh auth login`) as an account that may push to the repository.
- Python 3.14, as `python3`.
- Node 22, as `node`.
- Claude Code, as `claude`, signed in.
- Codex, as `codex`, only if you want Codex workers. It is optional.

`shipline install` checks each of these. It says exactly what is missing, and then it changes
nothing.

## Get Shipline

Shipline is a checkout of its own. Your repository keeps none of its scripts.

```sh
git clone https://github.com/NazmievBulat/Shipline.git ~/.local/share/shipline
```

## Set up your repository

Work in the main checkout of your repository, on `main`.

Write `shipline.json` at its root. It names the repository on GitHub and the command that runs
its tests. Every other key has a default; `scripts/workflow/README.md` in Shipline lists them.

```json
{
  "repository": "example-org/example-app",
  "commands": {
    "test": "python3 -m unittest"
  }
}
```

Then add the CI workflow, the rules the sessions follow, and the folders Shipline keeps its
notes and worktrees in, and push them to `main`:

```sh
mkdir -p .github/workflows
cp ~/.local/share/shipline/examples/ci.yml .github/workflows/ci.yml
cat ~/.local/share/shipline/scripts/workflow/RULES.md >> AGENTS.md
printf '.local/\n.claude/\n' >> .gitignore
git add shipline.json .github/workflows/ci.yml AGENTS.md .gitignore
git commit -m "ci: work through Shipline"
git push origin main
```

The CI workflow runs your tests on GitHub's own runners. It also writes the notes the merge gate
reads to know what CI tested. The sessions read their rules from `AGENTS.md` on `main`.

## Install it for your repository

```sh
~/.local/share/shipline/bin/shipline install .
```

This creates the labels `workflow`, `audited` and the four priority labels on GitHub, when
they are missing. It writes a systemd user timer, `workflow-tick`, that runs the tick on your
repository every 3 minutes, and starts it. It adds the hooks the sessions report through to
`~/.claude/settings.json`. It links `~/.local/bin/shipline` to Shipline's launcher; keep
`~/.local/bin` on your `PATH`.

Check that the timer runs:

```sh
systemctl --user list-timers workflow-tick.timer
```

## Open a first ticket

A ticket is a draft pull request labelled `workflow`. Its description is the whole task: what
to do, and what done means.

```sh
git switch -c ticket/hello
git commit --allow-empty -m "docs: the README says hello (ticket)"
git push -u origin ticket/hello
gh pr create --draft --label workflow --title "docs: the README says hello" --body "Add the line Hello. at the end of README.md. Done means: README.md ends with Hello. and the tests pass."
git switch main
```

## Watch it go through

On its next run the tick starts a worker, `ticket-N`, for the draft. The worker builds the
change, pushes it and marks the pull request ready. The tick then starts a fresh reviewer,
`review-N`. When CI is green and the review passes, the reviewer hands the merge to the tick,
and the tick merges it through the merge gate as one squashed commit.

See the sessions that run, and what each is doing:

```sh
shipline session list
```

Each session has a link you can open in the Claude app. A session that needs you asks there.

## Stop it

```sh
systemctl --user disable --now workflow-tick.timer
```
