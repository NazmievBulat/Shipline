# Nudge rules

The tick (`scripts/workflow/tick`) watches the screens of the sessions every pass and sends the
same few messages by rule. This table is the whole record of what it types, when, and what it
leaves alone, so that a repository needs no model session that walks the screens to nudge. What
needs judgment stays with the orchestrator, which the wake tells.

Every message is typed with `session <command>`: the text goes into an empty input box, and the
key that sends it follows only when the box holds exactly that text. Each one is logged in
`~/.local/state/workflow/wake.log`.

## Left alone by every rule

- Text in the input box that the tick did not type there itself.
- A question to the owner: an owner-blocked or interaction-blocked note, or a last reply that
  asks him (a line ending in `?`, or words to type back such as `reply 1 or 2`).
- A session stopped on the usage limit, until its reset (then only the resend below).
- A session that said its finishing words (`worker finished`, `review finished`, `review sent
  back`, `audit finished`).
- A session given another message by the tick in the same pass.

## Messages

| Case | Sessions | Wait | Text | Also left alone |
| --- | --- | --- | --- | --- |
| Stopped on the usage limit | workers, reviewers, audits | until the reset its limit line names, once per reset | the session's own instruction again | a hunt; a line with no time; another limit's hour with no day the usage figures cannot confirm |
| Checks finished | a reviewer with a check-wait note, or idle with its own round on the head; an audit in the merge train | all checks on the head finished, once per head (in the line: once per turn, state of main and head) | "The checks on `<head>` have finished. Read them once and go on by the rules: merge when they all passed, fix or send back when one failed." | a reviewer whose pull request is not first in the line |
| Merge conflict | the same | GitHub reports a conflict on the head, once per head | "GitHub reports a merge conflict on `<head>` (CONFLICTING or DIRTY). Merge current main into this branch by the rules, ..." | |
| Lost check | the same | the merge gate noted a check GitHub lost, once per head | "GitHub lost a check on `<head>`: ... Call the merge gate again by the rules; ..." | |
| Merged in a batch | a reviewer or audit | another member's gate call merged its pull request, once | "Pull request N was merged by the merge train, in a batch (commit `<squash>`). Do not call the gate again. ..." | |
| Waiting for its own job or turn | Claude workers and reviewers | idle 50 minutes after `notify waiting`, or a reviewer idle for its turn in the line; once per idle stretch | "Status check: if what you were waiting for is done, carry on; if not, run `scripts/workflow/notify waiting` and end your turn." | one with an asking note |
| Ready but not finished | Claude workers | idle 15 minutes on a pull request ready as long, without `worker finished`; once per ready stretch | "Pull request N is ready, but you have not finished. Finish by the rules: ..." | a draft pull request |
| Still screen | busy Claude workers and reviewers | the screen, without the box, the status line, the spinner and its clock, unchanged for 45 minutes since it last changed or the session turned busy; once per stretch and head | Escape, then "The command has produced nothing for 45 minutes. Check whether it is still running and go on." | a permission prompt; a session that ended its turn to wait (idle, job-wait or check-wait note); a message queued under the spinner (a `❯ ` line after the spinner, above the box); any text in the box; after Escape, a message it put back into the box (nothing is typed) |
| Wait loop finds itself | busy Claude workers and reviewers | the `pgrep -f` or `ps \| grep` loop it started last matches its own shell, and the screen has stood still for 5 minutes; once per loop | Escape, then "That loop finds its own command line and never ends. Check the process by its number." | the same as a still screen; a loop while another command runs, or while the screen moves (it is named when the screen stands still, the turn ends or 15 minutes have passed) |

A nudge whose Enter was not taken is sent again at the next passes, without another Escape,
while the box holds exactly that message, three times at most. Anything else in the box ends
the retry; after the third, the wake is told
`stuck? <session> (nudge not taken; its text stays in the box)`.

Codex bridges, hunts, audits and analysts get none of the last four.

## Named in the wake, not typed

| Case | Sessions | Wait | Wake line |
| --- | --- | --- | --- |
| Still screen | as above, not on a permission prompt | as above | `stuck? <session> (<min> min, running: <first line of the command>)`, ending `; nudge sent` when it was nudged, `; interrupted, text left in its box` when Escape put a message back, `; nudge failed` when typing failed |
| Wait loop finds itself | as above, busy or idle | once per loop | `wait loop finds itself? <session> (running: <loop>)`, with the same ending |
| Idle, no rule | Claude workers and reviewers | idle 50 minutes, with no wait note, no asking note, not a worker on a ready pull request; once per head | `idle, no rule: <session> (<min> min, last line: <line>)`, the line being the last words above the box, not the spinner or the time a turn took |
| Rule break | reviewers | said `review finished` without handing its merge to the tick; once per head | `rule break: review-N said review finished without a merge handoff` |
| Asking the owner | any session whose stop rang the phone with a question | until he answers | `asking the owner: <session> (<min> min)` |

The tick types nothing it has no rule for. Whether an unusual wait is reasonable, and what to
do with an idle session no rule covers, is the orchestrator's call.

`session list` shows, for each session the tick has looked at, when its screen last changed:
`screen changed <Mon D, YYYY, H:MMam> (UTC)`. The 45-minute rule counts from that time.
