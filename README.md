# Shipline

Shipline runs a software team of AI coding sessions: workers build tickets, independent
reviewers check them, and a merge line lands each change on a green main.

## Getting started

Shipline runs on one Linux machine, from a checkout of its own, and works on a repository on
GitHub that keeps none of its scripts. `shipline install` checks what it needs and sets it up.
[`docs/getting-started.md`](docs/getting-started.md) takes a repository from nothing to its
first pull request merged through Shipline.

## How it is built

Every change to Shipline is a ticket: a draft pull request that a cloud session builds and an
independent reviewer checks and merges. The rules for building and reviewing a change here are
in [`AGENTS.md`](AGENTS.md).

## License

Apache-2.0 (see [`LICENSE`](LICENSE)).
