# Workflow workers

The tick reads `WORKFLOW_WORKER_PROGRAM` from its environment first, then
`.local/workflow/worker-program` in the main checkout. The default is `claude`.
Set the environment value to `cloud`, or put the single word `cloud` in that file,
to start eligible labelled draft pull requests in the cloud at medium effort.
Use `claude` to switch back to local Claude, or `codex` for local Codex. Remove an
environment override before changing the file; the environment takes precedence.
Changing the setting affects future starts, leaving running sessions alone.

Cloud uses the same screen exclusions as Codex: screen changes and unclear tasks
with no diff stay with local Claude. Test IDs and data-only route lists retain the
existing exceptions. A cloud-built ticket returns to the cloud for a fix round,
even after the default changes, subject to the same screen exclusions.
Independent reviewers always stay local.

Cloud starts bypass the local memory and load checks. The tick waits when the
5-hour usage exceeds 85% with more than an hour until reset, or usage is unreadable.
An existing cloud reservation prevents a local worker or reviewer from starting.
A launch refused before Claude was invoked is retried on a later tick, keeping
its reservation. A missing View address after invocation is ambiguous: the tick
keeps the reservation and retries address recovery from the saved launch output
and pane. It never creates a second session to replace an unknown one. Failures
are reported once through the wake, with pending reports retained until delivery.

The owner answers cloud questions in the cloud session. Its reports are relayed
from pull request comments, and its completion releases the reservation only once
the pull request is ready. `session list` shows addresses and report ages.

Dependency 614 supplies repository-local security rules for cloud pushes. The
orchestrator records the first three cloud tickets' independent review round counts
against Claude-built tickets and the PC's observed load in their descriptions;
those measurements require real workers after rollout and must not be estimated.
