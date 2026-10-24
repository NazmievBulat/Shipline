# Shipline

Shipline runs a software team of AI coding sessions: workers build tickets, independent
reviewers check them, and a merge line lands each change on a green main.

## How it is built

Every change to Shipline is a ticket: a draft pull request that a cloud session builds and an
independent reviewer checks and merges. The rules for building and reviewing a change here are
in [`AGENTS.md`](AGENTS.md).

## License

Apache-2.0 (see [`LICENSE`](LICENSE)).
