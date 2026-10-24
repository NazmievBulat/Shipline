This file holds the rules for repositories that use Shipline; Shipline's own development follows `AGENTS.md` in the Shipline repository.

# Workflow sessions

A draft pull request labelled `workflow` gets a worker session, which builds it and marks it
ready. The tick then starts a fresh Claude reviewer, which reviews it and merges it. An audit session is not a stage every pull request passes: it is only for a pull
request the orchestrator names, one that was built before the workflow existed. A session is
started by `shipline session` with one sentence as its whole instruction. This file
says what each sentence means, and what the orchestrator session does.

A third kind of session has no pull request: a hunt, which uses the running application in a
browser and reports the bugs it finds. It is started by hand with a sentence of its own, and
the repository's hunt rules are its whole instruction.

Two hunts may run at once, each in its own area of the application, never three. A repository
names its areas in `shipline.json`, under `hunts.areas`: each a short name and the screens
(paths) it covers. A hunt started with an area is told its area and its screens in its
sentence; it stays off the other areas' screens except to pass through them. A hunt without
an area hunts every screen and runs alone. With no areas configured, one hunt runs at a time.
In the hunt plans file (`hunts.plans`, which the sentence of a hunt with a plan names), each
task starts with its area or "Every screen."; a hunt with an area takes only the tasks of its
area.

A fourth kind has no pull request either: the analyst, a read-only session the tick starts once
a day, which reads the workflow's own logs and proposes at most three changes to it (see
"Analyse the workflow").

The rules of a session are this file as it is on `main`, not the copy in its worktree: the
worktree holds the branch of the pull request, and a branch made before a rule changed has the
old rule, or none. The sentence a session is started with says so and names the commands:
`git fetch origin main`, then `git show origin/main:AGENTS.md`. A session reads that first and
follows it wherever the two copies differ. The files that hold rules for the code itself
are read from the branch, with the code they describe.

Rules for the code itself are in the repository's own rules files, beside the code they
describe.

Never done without the owner: a force-push, a change to text that is sent to a model, and any
product decision (a limit, a rule, a wording, a screen).

A worker and its independent reviewer do not communicate. The reviewer reads the task, diff,
commits, code and these rules, never the builder's transcript, logs, reasoning or own review.
An audit remains only for a pull request built before this workflow, named by the orchestrator.

Every session that merges uses the merge gate, from its pull request's worktree:
`shipline merge-when-current N`.
Never call `gh pr merge` directly. The gate holds one machine-wide lock through the freshness
check and squash merge, checks main's latest CI result and image checks of its pushes (a
scheduled image run is only reported in the wake when it fails), and verifies that
the latest green pull request CI tested a merge whose parents are the current main and the
current pull request head. Images, when needed, must have passed on that same tree, and so
must the whole browser suite and every server test: a pull request's own run of the
specs or the server tests its change reaches does not count. When its own E2E run was not the
whole suite, or its own CI ran a selection of the server tests, the gate dispatches E2E, CI or
both on a scratch branch holding the tested merge, as for a batch, and merges once those runs
passed; while they run, later pull requests may go first as while checks run, and a failed one
is a failed check until a rerun of it passes. A change that needs no browser test, or no
server test job, needs no run of that suite. A red, pending or unreadable main stops merging, with one exception: on a red main, a
pull request whose fresh CI on that same main ran and passed every check failing on main merges, because it
is the fix (a skipped check does not count). An advisory on main is not a red main: when main's
dependency audit (`checks.dependency_audit` in `shipline.json`) is its only failed check, its
other checks passed, and the audit's notice (`checks.advisory_annotation`) names advisories
alone, main is `advisory`, merging goes on, and the update that fixes the advisory counts as the
fix of a red main once its latest CI passed with its own audit clean, whatever main that CI
tested; its merge is verified as any other's. The pending of each merge on an advisory main does
not end that state. A failure of the audit for any other reason, a lock file it cannot audit, a
tool error or an advisory service that did not answer, leaves main red. A pull request's own
audit still refuses an advisory main does not have, and passes over one main has, with a
warning. After a squash, main inherits the green result
of the identical tree tested by the pull request CI for each CI job that run passed, and
images when every image job passed on that same tree; the gate records the source runs. A CI
job the pull request skipped, which main always runs, waits for main's own success, and
nothing merges meanwhile, so no later push cancels that run. The squash commit carries the
gate's proof (a `Workflow-Proof` line): main's CI and E2E skip a job that line shows passed,
whole, on the identical tree, and name the run they inherited.
Main's other checks still run, and any failure makes main red as before. A push to main the
gate did not make runs everything. A different tree waits for main's own checks, then the
gate is called again; a stale pull request is refreshed meanwhile. The tick reports a red, advisory or unreadable main in the
wake, including the failing checks and commits since the last green main, and again when it
leaves that state; green again lets merging go on.

Every ticket has a priority, one of four labels: `priority: blocker` stops every merge or push
now (a red main, a push refused for everyone, a deadlocked train); `priority: high` removes a
bottleneck (CI capacity, a flaky test, a stalled or slow workflow); `priority: medium` is
anything else; `priority: low` only when the owner says so. A pull request without one counts
as medium, and one with several as the most urgent of them. The fix of a red main, an advisory's
update included, counts as a blocker whatever its label, and goes before every other blocker.

Ready pull requests merge in queue order, in a merge train: by priority first (blocker, high,
medium, low), then in the order they joined within one priority. A pull request whose priority
label changes while it waits moves at the next turn. The gate tests the first members of
the line at once, each as a prefix: main and every member ahead of it, merged with this one,
on a scratch branch of its own, with its CI, the whole browser suite's E2E run and any
required image checks. How many it tests, the window, starts at 4, grows by one after each
merge and halves after a failed prefix, never below 1 and never above 4 (`WINDOW_CEILING` in
`scripts/workflow/merge_gate.py`). A member merges as soon as its own prefix passed and every
member ahead of it merged; its squash must match its prefix's tree, and main inherits that
prefix's green result. A first member whose own CI tested its merge with the current main
merges by its own checks and needs no prefix. Each member must still have a passed review
and a gate call on its current head. A member whose prefix failed once every member ahead of
it merged is blocked as a failed check, and its own gate call reports it; the prefixes behind
it hold its change, so they are built again from the new front without it, and their failure
is not their members' own. A green rerun of that failed run, on the tree main has now,
restores its place and is its next prefix. A member that conflicts with one ahead of it waits
for its own turn. A main changed from outside the gate, a changed or unready member, or a
line now served in another order drops the prefixes from the first one that no longer
stands, and they are built again. A scratch run that passed without proving
its tested files (a browser run that was not the whole suite, notes naming other files, a
scratch branch changed under it), for a prefix or a single pull request, is a wait: the next
call retests on a new scratch branch and the hand-offs stay. The same shortfall twice in a
row on the same tree is a refusal. A member needing a check that the scratch
run cannot perform, such as an older version's compatibility with a new database schema,
follows the single path,
and no prefix starts behind it before it merged.
The session whose gate call merged several members does the after-merge duties of its own
pull request only. The tick tells the reviewer or audit of each other member that its pull
request merged, once, and that session does its own after-merge duties and finishes; a gate
call on it says the same. For a member handed off to the tick, the tick does its after-merge
duties instead. The gate deletes its scratch branches when no saved prefix or rerun needs them.
Calling the gate says that the
review passed, and puts the pull request at the back of its priority in the line; the line is
kept in `.local/workflow/merge-train.json` of the main checkout, and
`merge-when-current --train-status` shows the first, the waiting in the order served, the
blocked, each member's priority, the turn, the window and each member's prefix with the state
of its runs.
The first eligible member refreshes or merges; when its checks are still running or queued,
later members with green checks tested on current main may go first, including by a prefix,
while it keeps its place and regains priority when green. A pull request keeps its place but
is passed over while
its checks failed on its head, while a conflict on its head waits to be resolved, after its
gate call found main red and it was not the fix (until main or its head changes), and while
it has no reviewer or audit that can act: none running, one stopped on the usage limit, or
one waiting for the owner. So a pull request waiting for the owner does not hold the train. A fix pushed after
failed checks joins at the back when the gate is called again; a green CI re-run on the same
head and current main lifts a failed-check block and restores the pull request's earlier
queue position, and a green re-run on an older main is refreshed in that place when its turn
comes. A re-run of only the failed jobs keeps the run's proof of the merge it tested, as a
full re-run does. A pull request that merged, closed, became a draft or left `main` as its
base leaves the line. One exception: a pull request whose failing test the tick gave to a
worker (see "Fix the failed check of pull request N") keeps its place as a blocked member, as
a draft and on its fix, until its fresh reviewer calls the gate; then it regains that place,
and is served there once its fix's CI is green. When the gate says the pull request waits behind another (or for main to change),
it has already run `shipline notify waiting` itself: end the turn. The tick wakes
that reviewer or audit when its pull request is first and all checks on its head have
finished, once for each turn of the line, state of main and head. One whose checks failed
is woken as any waiting reviewer is, when its checks finish. A reviewer hands its merge to
the tick instead of waiting in the line (see "Review pull request N"); these wakes are for
audits, and for reviewers that already waited in the line when that rule landed.

A check GitHub still lists as queued or running 10 minutes after its workflow run completed
is lost: no wait brings it, and until it is handled it counts as a check still running. When
every check not passing on the first member's head is lost so and the runs completed green,
its gate call pushes one empty commit from the clean pull request worktree
(`ci: run CI again, GitHub lost <check> of run <id>`), says so and exits without merging;
wait for that new CI run and call the gate again. One such push per head: a check lost again
on the pushed head, or lost on a run that did not pass, is refused and reported. The tick
wakes the first member's reviewer or audit for a lost check, once for each turn of the line,
state of main and head, or drives its handoff, and the wake tells the orchestrator
`lost check: <check> of run <id> on pull request N` at any hour, once per head.

A stale run is refreshed by merging current main into the clean pull request worktree,
clearing the ignored, untracked generated output listed in `refresh.clear_generated` in that
worktree before pushing, then the gate exits without merging the pull request. Wait for that
new CI run and call the gate again. Re-running an old workflow reuses the original merge commit and cannot
prove freshness. A run from before CI recorded its tested merge needs a fresh run too; if main
is already in the branch, the gate pushes an empty commit. A conflict is left for the session
to resolve, with `merge main: conflict in <files>` as the merge message. Never resolve a
non-mechanical conflict without the owner. A worker still never merges its own pull request.

## Asking the owner

The owner follows the sessions from his phone. There he can read and type, and nothing else:
the card of the question tool cannot be submitted, and he cannot run a command. So, for a
worker, reviewer and audit alike:

- A question to the owner is plain text at the end of a turn: the recommendation first, then
  the other choices, ending with the short words he can type back ("1", "merge it").
- Just before those words the question carries one line of its own: `Owner only: yes` or
  `Owner only: no`. `Owner only: yes` is required for money, privacy, legal text, text sent to
  a model, and any product decision these rules keep for the owner; anything else is
  `Owner only: no`. A question without the line counts as `Owner only: yes`, waits for him and
  is named to the orchestrator once.
- A question marked `Owner only: no` that has waited `answer_for_owner_after_minutes` in
  `shipline.json` (15 by default; 0 turns it off) gets its recommended reply from the tick:
  the first reply word after the line (so the recommended one comes first, a number or words
  in quotes, never a command such as `/clear`), typed into the session once, as if he had
  typed it.
  His own reply, if it comes first, wins; so does text he has not sent yet. The wake's morning
  check-in lists the questions answered for him overnight, with the reply given, so he can
  overrule them. A Codex bridge relays and is never answered for him.
- The question tool is not used.
- The owner is never handed a shell command to run. What has to be run, the session runs.
- A change to a screen still publishes the artifact with the options and gives the link; the
  text question at the end of the turn repeats the card's title, the choices one line each
  with reply words, and the link.
- An options page opens with a card readable in ten seconds, before any option or detail,
  in plain words: what a person meets today (with a hunt's picture or the steps when the
  ticket has them), what this change does in one or two sentences, and the one thing he chooses.
- The card's title says what a person meets, not the ticket's name, in at most ten words.
  Before and after are pictures side by side: today's screen (the hunt's picture when there
  is one) and the recommended option, each captioned in a few words.
- The card asks one question with at most three choices, each one line saying what it means
  for a person using the product, not how it is built. The recommendation is first and marked;
  each choice has its reply word ("Reply 1").
- Everything else goes below the card: the clickable options, built with the real components,
  the reasons and the details. Nothing needed for the decision is only down there.
- When the permission check refuses a session's own merge gate, label or change of a
  description, the session asks him in one plain line for his word in that session (for
  example: the merge of pull request N was refused, write "merge it" and I run it). When he
  has written it, the session runs the same command again itself.

## "Implement pull request N"

- Read the description with `gh pr view N --json title,body`. It is the task. Read the latest
  independent review whose body begins `<!-- workflow-review round=` from
  `gh api --paginate repos/NazmievBulat/Shipline/pulls/N/reviews`, if any. A sent-back
  worker fixes every finding there before marking ready again.
- Start with brainstorming. A change to a screen publishes an options page (as in "Asking the
  owner") and ends the turn with the card's title, choices, reply words and link in text;
  nothing is built until the owner answers. A backend choice that is a product decision is
  asked in text, recommendation first, with the `Owner only:` line of "Asking the owner".
  Any other choice is made and written into the pull request description.
- Run the repository's fast pre-train checks, `shipline pre-train-checks`, from the pull
  request's worktree before marking ready. They also run before a handoff or gate call
  admits it to the merge train; a failure refuses admission. They supplement CI, which still
  runs its normal checks.
- A draft pull request runs no CI: its run starts when it is marked ready, on the head it then
  has, and each later push runs again. So run the tests in the session before marking ready.
- Run `shipline weaker-tests --base <base> --working-tree` before marking ready.
  Explain each item in the description: why the change is correct and how the test still
  catches its original bug, ideally with a failing run on a deliberately broken version.
- Commit with conventional commits and push the branch. Never force-push.
- Before a browser test run, `shipline session protect ticket-N`; after it,
  `shipline session unprotect ticket-N`.
- To finish, run one round each of the test coverage reviewer and `codex review --base <base>`,
  and test what was built, using the repository's local browser test selection,
  `shipline browser-tests-for-changes <base>`; when the repository configures none, it says
  so, nothing is selected and CI runs the whole suite. Fix the
  findings and run
  one round on those fixes. Then mark ready and stop; the independent reviewer does the deep
  check. A job longer than this pull request
  becomes a new draft ticket as in audit step 5, without the label `workflow`.
- A worker never merges its own pull request or changes another pull request or its labels.
- A pull request already ready when the worker starts is left ready; stop without rebuilding.
- When ready, name each ticket pull request this session opened, by number and title, and say
  that it waits for its start by the tick. Then say "worker finished" on a line of its own.

### Codex workers

`session start worker N [level] codex` starts interactive Codex in `codex-N`, with the
same worktree and sandbox as the worker, and a Claude Haiku 5.5 bridge in `ticket-N` at low effort.
The bridge is the Remote Control session the owner sees. It has only two relay tools: wait
for Codex's turn and type the owner's reply into Codex. It never edits, tests, pushes, merges,
labels, decides or answers for anyone. It shows Codex's last message word for word, then ends
its turn; a direct reply from the owner is typed unchanged into Codex. Permission questions
that pause Codex inside a turn are also relayed, with the confirmation choices unchanged. A message from another
session is shown to the owner, never typed into Codex. Workers ask in plain text as above.

Codex's notify payload records a turn for the bridge; the bridge's ordinary hooks ring the
phone only on a question, with silence on completion, waiting for a background job and usage
limits. The bridge and Codex are stopped together on completion; list, squeeze, protect, unprotect, stop,
resend and cleanup apply to both. A bridge that ended while Codex runs is started again by
the tick (`session bridge ticket-N`); it shows Codex's last turn unless the owner answered
it, and Codex is never stopped for it. Codex's own dated quota error is read from its pane;
a limit without a readable reset waits for the orchestrator. Codex's usage is never inferred
from Claude's usage figures.

The launch sets Codex's `notify` for that process only. `install-hooks` installs Claude hooks
only and does not change `~/.codex/config.toml`. A callback outside the worker's bridge does
nothing. The bridge records direct owner prompts and sends each recorded reply at most once;
automatic wakes and messages from other sessions cannot authorize a reply. Codex's PATH starts
with a `gh` shim that permits reads, `gh pr ready N` for its own N only without `--undo`,
`gh api -X PATCH repos/NazmievBulat/Shipline/pulls/N -F body=@file` for its own description,
and `gh pr create --draft` for follow-up tickets, with no label but `priority: medium` or
`priority: high` (see audit step 5). It prevents mistakes; the
real `gh` remains reachable. The reply tool takes no text: it sends the recorded owner prompt,
including its whitespace, and notes which delivered turn the owner answered.

The tick uses Claude unless the owner sets `WORKFLOW_WORKER_PROGRAM=codex` in its environment
or writes `codex` to `.local/workflow/worker-program` in the main checkout. `claude` switches
it back. After this lands, the orchestrator starts three small tickets on Codex first (for
example 185, 186 and 187), compares their review round counts with Claude-built tickets,
records measured bridge cost in their descriptions, and reports to the owner before changing
the default. No trial cost is guessed in advance. Even with the Codex setting, the tick gives
Claude any ticket whose diff changes the screen folders (`web/` or `ui/`) beyond test ids or a
data-only
route list. With an empty diff it judges the description conservatively; an unclear scope
stays with Claude. A sent-back ticket with a `codex-built-N` marker gets Codex for its fix round regardless of the default program, using the same screen exclusions above, which still get Claude.
A ticket whose description has the line `Worker: Codex (a bug fix with no new UI)` on its own
is eligible for Codex whatever its diff or its words, its fix rounds included. Only the
orchestrator writes that line, with `shipline decide program N codex`.

Read the task, including any `Answers given for the owner`. Put unanswered product decisions
under `Questions for the owner` in the description, recommendation first, and ask in the bridge
in plain text. Wait for the answer before building. For screens, build clickable options as
components (a component preview or an options page under `.local/`), opening with the
ten-second card
as in "Asking the owner"; the orchestrator or Claude reviewer publishes the artifact and asks
the owner. Do not build the screen until the answer is given.
Codex uses the same local browser test selection and worker cap.
Codex pushes its branch and marks its pull request ready; it never merges, labels or changes
another pull request. It closes with the same ticket list and `worker finished` as Claude.

### Cloud workers

Set `WORKFLOW_WORKER_PROGRAM=cloud` in the tick's environment, or write `cloud` to
`.local/workflow/worker-program` in the main checkout, to start labelled drafts in the cloud
at medium effort. `claude` switches back to local Claude; `codex` keeps its existing meaning.
Cloud starts use Codex's screen exclusions and conservative empty-diff routing; excluded
tickets stay with local Claude. A `cloud-built-N` marker keeps sent-back cloud tickets in
the cloud for fix rounds, with those same exclusions. Reviewers stay local.
The tick starts no cloud worker above 85% of the 5-hour window unless it resets within an
hour; unread usage waits too. Cloud starts do not wait for local memory or load.

Failed launches keep their reservation and are named once in the wake. A later tick retries
only a launch confirmed not to have invoked Claude. For a missing View address after Claude
was invoked, it retries address recovery from saved output or the launch pane, never blind
creation of a replacement session. This also applies to records from older launches.

`session start worker N [level] cloud` launches a cloud worker in a short-lived tmux pane.
Its `View:` address is kept in `.local/workflow/cloud-N` of the main checkout. While that
record exists, the tick starts no local worker or reviewer for that pull request, whatever
its label. `session list` shows `cloud-N`, its address and the age of its last report.

The cloud worker uses its own branch and setup, runs a database server as a separate
unprivileged user when needed and long suites in the background, and uses a reviewer
subagent when
`codex review` would run locally. It pushes only to its pull request's branch, never
force-pushes and never skips or weakens the pre-push hook. It reports the start, first push,
blockers with their exact errors, owner questions and ready by pull request comments beginning with
`<!-- cloud-worker -->`. A question ends as "Asking the owner" says: the choices one line each,
each beginning with its reply word, the line `Owner only: yes` or `Owner only: no`, and the
reply words, the recommended one first. An answer it is given in its session it reports on the
pull request before it carries on. It never ends its turn to wait for an answer; it polls the
pull request's comments every three minutes instead. The tick relays the reports without a
model: a final question rings the phone once through `notify` and appears in the wake as `asking
the owner: cloud-N`; a `Blocked` report is told to the orchestrator once. A question marked
`Owner only: no` with no answer after `answer_for_owner_after_minutes` gets its recommended
reply as in "Asking the owner", once for the same words, in a comment of the tick,
`<!-- shipline-orchestrator --> answered for the owner: <reply word>`, quoting that choice's line
when it has one, and its worker is woken as after any answer. An answer from the owner or the
orchestrator that comes first wins, and only a question from an account that may write to the
repository is answered. One without the line, or without a reply word, waits for the owner and
is named in the wake once; the morning check-in lists the answers. A final `worker finished`
removes the record only once the pull request is ready, letting the normal independent review
start. Reports and failed deliveries are remembered locally. The workflow does not read cloud
transcripts or stop cloud sessions; the owner answers in the cloud session or on the pull
request.

A cloud session that ends its turn stays stopped until something types into it. The tick keeps,
in each record, when it first saw the draft's head and the time of the last report. A worker
whose draft has had no push and no report for `cloud_wake_after_minutes` of `shipline.json`
(30 by default; 0 turns waking off), and no open question (a question report with no later
answer), is woken once per quiet stretch: the tick attaches to its session in a short-lived
tmux pane (`claude --cloud <address>`), waits up to 60 seconds for the input box, checks that it
is empty, types a status check, sends it a second later and ends the pane, which ends only the
local client. The wake log says `woke cloud-N (30 min quiet)`. Only comments from an account
that may write to the repository count, as reports and as answers: anyone may comment on a
public pull request. An answer to its question, a comment starting
`<!-- shipline-orchestrator -->` or an unmarked one (a reply quoting the question counts; a
command to a bot such as `@codex review`, or any comment starting with `@`, does not), wakes it
at once the same way. A worker still quiet that long
after its wake is named in the wake once, `stuck? cloud-N (60 min, woken once)`, and is not
woken again in that stretch. An attach that fails (no box, an ended or expired session, a usage
limit shown, text already in the box) is logged once and never tried again in that stretch.

## "Fix the failed check of pull request N: job J of run R, first failing test T"

A worker the tick starts at medium effort when a test of the pull request's own failed on its
handed-off head, usually after main was merged in. It is the worker of "Implement pull request
N" with these differences:

- The task is the failed check. Read the description, then the failed log of run R with
  `gh run view R --log-failed`. T is "not reported" when no test line could be read.
- Find why the test fails on this head and fix it within the task of the pull request: the
  code, or a test whose expectation main's change made wrong. Never weaken or skip a test to
  make it pass. Run that test and the repository's fast pre-train checks.
- There is no review to fix and no brainstorming; a fix that needs a product decision, or more
  than a fix, is asked as any worker asks.
- Commit, push without force, mark ready and say "worker finished". The pull request kept its
  place in the merge train; a fresh reviewer reviews the fix as a normal round and hands it
  off, and it is served in that place once its CI is green.

## "Review pull request N"

A fresh context, always Claude Opus, started by `session start review N`. It runs at medium
effort, and at high when the change touches a path `review.high_effort_paths` of
`shipline.json` names (stored data, text sent to a model, background work, locks and queues,
account boundaries and security) or one of Shipline's own concurrent scripts, or when the
description has the line `Review: high` on its own; `review-effort N` says which and why, and
says high whenever it cannot tell.
It knows only the description (the task), diff against the base, commits, code and rules
from main. Do not read worker logs, transcripts, own reviews or ask the worker for a handover.
Local `.local/workflow` session files are control notes, not review evidence. Panel reports
under `.local/workflow/reviews/N/<head>/` are independent review evidence for that head.

1. Read the description and whole diff. Run the panel of the main checkout, which a branch
   made before it existed does not have:
   `shipline review-panel N`.
   The independent reviewers are Cursor and Codex for a pull request Claude built, and
   Claude and Cursor for a pull request Codex built. Claude is the reviewing session;
   the panel runs Cursor and, when eligible, Codex in separate detached worktrees at the
   published head, with `.review/task.md`, `.review/diff.patch`, `.review/weaker-tests.md`
   and the main version of the workflow rules in `.review/workflow-rules.md`, read-only under
   a filesystem sandbox, each
   with a 30-minute timeout. Codex is skipped on any pull request it built, recorded by
   `session start worker` in `.local/workflow/codex-built-N` of the main checkout. A missing,
   signed-out or failed tool is noted and skipped. Cursor and Codex run in parallel.
   Read `task-as-opened.md` and `task-changes.md` beside the panel reports. A requirement
   removed or narrowed since opening is a finding unless `Answers given for the owner`,
   or the orchestrator's answer under his standing delegation, covers it. If history cannot
   be read, the panel notes it and review continues with the current description. Each
   panel reviewer receives these files in `.review/` too.
   Read `weaker-tests.md` and only the completed `cursor.md` and `codex.md` report files
   available for the current head. The panel writes the advisory weaker-tests report next to
   those reports and gives each panel reviewer a copy in `.review/`. For every item, explain
   in the published independent review why the change is correct and how the test still
   catches its original bug, ideally with proof that it fails on a deliberately broken
   version. An item without that reason is a finding: fix it here or send it back.
   The report alone never fails CI. You are the only writer: check findings against the code,
   discard unsupported ones and count duplicates once. Each report includes coverage verdicts for unit, black-box API and browser tests,
   with at most three ranked gaps per level, naming file:line, breaking input and needed
   test. A gap in a task requirement or bug fix counts toward sending back; optional extra
   coverage does not. Judge coverage yourself too; run `codex review --base <base>` only
   when Codex did not build this pull request.
   Test what was built with the repository's local browser test selection
   (`shipline browser-tests-for-changes <base>`); use the browser
   by hand for
   screens, protecting `review-N` before and unprotecting it after the run.
2. Read only earlier independent reviews for this pull request from
   `gh api --paginate repos/NazmievBulat/Shipline/pulls/N/reviews`. Each round has a review
   body beginning `<!-- workflow-review round=N result=sent-back -->` or
   `<!-- workflow-review round=N result=passed -->` (N here is the round number). The first
   round is 1; a new reviewer uses one above the highest published round. If a round was sent
   back, judge whether every finding was fixed and what those fixes broke; do not repeat an
   unrelated whole-ticket hunt. Record every round, even when you fix its small findings,
   using a COMMENT review with `commit_id` set to the reviewed head. Never count Codex's or
   the builder's reviews as independent rounds.
3. Fix small, clear findings yourself, commit and push without force. Rerun the checks on
   those fixes. Anything larger is sent back: publish a COMMENT review via
   `gh api -X POST repos/NazmievBulat/Shipline/pulls/N/reviews` with the round marker,
   evidence, ranked findings and what proves each fixed. Use COMMENT because the owner may
   also be the pull request author. Then `gh pr ready N --undo`. Only after both succeed,
   end with `review sent back` on its own line. The tick stops this reviewer and starts a
   worker; the worker reads the latest marked review and fixes every finding before marking
   ready. The next reviewer is a fresh context, never a previous reviewer brought back.
4. Two sent-back rounds are allowed. Passed rounds do not count toward that gate: a stack
   member that passed round 2 may get a fresh reviewer after its base merges or its head
   changes. Markers published before outcomes were recorded retain their previous gate
   behavior; new markers always state the outcome. After two send-backs the tick requires
   the owner's allowance for the next round.
   If that round still finds problems, stop and ask the owner in text instead of continuing
   to fix or send back silently. Keep the session open without either completion line.
   A restart does not reset the count: read the published reviews. After the owner authorizes another round, the
   orchestrator records it with `shipline decide allowance N --round R --owner "<his words>"`, which
   writes its number to `.local/workflow/review-N.allow-round` in the main checkout. This
   permits that exact round only. Record the authorization with its review.
5. Ask product decisions in text, recommendation first, with the `Owner only:` line of
   "Asking the owner". A job longer than this pull request
   becomes a draft ticket as in audit step 5, without the label `workflow`. No question or
   unaddressed finding may remain before merging.
   When a watch for GitHub checks was stopped, run `shipline notify waiting` before
   ending the turn to wait for checks, instead of asking the owner for "go on". The tick
   wakes that reviewer once per head when all checks on the head have finished, including
   failed checks, even if the reviewer has not published its round yet. A reviewer does not
   wait in the merge train: its merge is handed to the tick (below). Without a check-wait
   note, an idle reviewer is eligible only if it published a round after its session started,
   on the head or on a commit the head grew from (its own fixes, a merge of `main`). It never
   answers an owner question or a refused merge; do not use `notify waiting` when asking the
   owner something.
- The pull request is merged through the merge gate as one squashed commit, by the tick, once
  all three hold: the checks on GitHub pass; the base is `main`; no question to the owner is
  open. The label `audited` is not needed. A check that
  fails is a finding like any other: fixed here, then waited for again. A check that has not
  started yet is waited for, not read as passing. The CI check is every check whose name
  begins with "CI": "CI: what to run", the jobs it starts and "CI result",
  which passes only when
  each of them that ran passed. One listed as skipped while "CI: what to run" passed was not
  needed. The image check is every check whose name begins with "Docker images": "what to
  run" and the jobs it starts. Each of them that ran must pass. One listed as skipped while
  "Docker images: what to run" passed was not needed, and is not waited for. The image
  check does not run for a draft: a pull request that changes what goes into the images lists
  all of them as skipped until it is marked ready, and then a new run starts. Those skipped
  checks are not a pass; the run that started after the pull request was marked ready is
  waited for.
  When they hold, the reviewer publishes a passed independent round on the current head and,
  with no question or finding left, always hands the merge to the tick; it does not call the
  gate to merge and does not wait in the merge train:
  `shipline merge-when-current --handoff N`.
  Run it from the clean pull request worktree, based on `main`. It verifies the published
  round, records `.local/workflow/merge-wanted-N` in the main checkout with its head and
  round, and joins the same merge train. Only after that succeeds, end with `review finished`.
  A reviewer of a pull request based on `main` that ends with `review finished` without a
  handoff and without a merge breaks this rule; the wake tells the orchestrator once.
  The tick keeps the worktree, retries expected waits, refreshes through the gate and merges
  through the gate, including batches. A gate refresh advances the note to its own pushed
  head; any other head change, a failed check, a conflict or another refusal revokes the
  handoff and starts a fresh reviewer. A failed check that is the pull request's own failing
  test goes to a worker instead (see "For the orchestrator session"). Main being red or pending still follows the gate's
  existing stop and fix rules. Stacked pull requests are moved mechanically as below;
  anything requiring judgment is returned to their own session or a fresh reviewer.
- A branch merges `main` in only when it needs to, not because `main` moved: when GitHub
  reports a conflict (`gh pr view N --json mergeable,mergeStateStatus` says `CONFLICTING` or
  `DIRTY`), when the pull request below it merged (the next point), or when `main` changed a
  file the branch changes or depends on, or the merge gate requires fresh CI on current main.
  Each such merge is a push, and on a pull request that
  changes the images each push sends the image check to the back of its runners' queue. A
  ready pull request that merges `main` in because of a conflict says so in the
  commit message (`merge main: conflict in <files>`).
- After the merge, for each pull request built on this one: set its base to `main` and merge
  `main` into its branch. Ask about a conflict that is not mechanical. A ready one labelled
  `workflow` is left to its own fresh reviewer (the tick starts it when its head changes;
  otherwise the orchestrator starts `session start review N` to finish its merge). A draft
  is left to its worker, and one without the label to its audit. Never merge a different pull
  request without that pull request's independent review.

6. A pull request whose base is not `main` is reviewed and left ready, without merging.
   When it is merged, or reviewed and waiting for its base, name each ticket pull request
   this session opened and say it waits for its start by the tick. End with `review finished`
   on a line of its own. The tick remembers a reviewed head so a waiting stack member does
   not get reviewed repeatedly. The orchestrator starts a fresh reviewer to finish its merge
   once the base moves to main, even if that did not change its head.

## "Review conflict resolution for pull request N extending passed head H in files F"

The tick starts this fresh Claude Opus reviewer at high effort only after a non-mechanical
conflict on a handed-off pull request with a passed independent round on H. F names every
unmerged file in the kept worktree. Read the task, the earlier passed round on H, and the
current rules from main. The earlier round stands for everything the resolution does not
touch; do not repeat its whole-change review.

Resolve the merge in the kept worktree, asking the owner in text for any product decision.
Commit as `merge main: conflict in <files>` and push without force. Run the main checkout's
`review-panel N`: it verifies that this is one merge of H and the recorded main, and gives
the panel the diffs against both parents for F, with the task and earlier passed round.
Read its completed reports, narrowed weaker-tests report and reached test selections. Run
every selected server test (`server-tests.json`) and the local browser test selection
(`browser-tests.txt`, empty when the repository configures none, and CI then runs the whole
browser suite), protecting `review-N`
before and unprotecting it after browser runs. The usual cap of three local browser test
files and two workers still applies; CI runs the rest. For a changed screen, use the
browser by hand too. The panel's local browser selection is limited to the files listed in
`<panel-folder>/conflict-files.txt`.

If no passed round on H can be proved, or the resolution changes behaviour beyond the
conflicting hunks (new logic, a changed test expectation, or another file), say why and
follow the ordinary full review instead, running `review-panel N --full-review` to get
fresh full-diff reports in the panel's `full-review/` folder. The panel also falls back when it
cannot prove the scope, including changed parents, additional commits, or non-text conflicts.
The gate refuses a narrowed handoff which changes another file or lines outside the hunks.

Publish the next usual independent round on the resolved head, stating that it is a conflict
round and naming H, with `<!-- workflow-conflict extends=H -->` on its own line after the
usual round marker. Explain why the resolution stays within scope and record test evidence.
A full-review fallback uses the usual round marker without that conflict marker and says
which fallback applied and why. The ordinary send-back count, checks, questions and waiting
rules still apply. With no findings or questions left and GitHub checks passing, finish
with the usual `merge-when-current --handoff N` and `review finished`. Never merge directly.

## "Audit pull request N"

A separate session, started only for a pull request the orchestrator names: one that was built
before the workflow existed. A pull request built by a worker gets no audit.

1. Read the whole diff against its base. Run `codex review` on the whole pull request and the
   test coverage reviewer (one verdict each for unit, API and browser tests, with ranked gaps).
2. Check the pull requests stacked on this one and, where the change touches data that
   another feature reads, its contract against what that feature requires.
3. A small finding is fixed on this branch and pushed. Codex runs again on the fixes.
4. A finding that needs a product decision is asked in text, recommendation first. The audit
   does not finish until it is answered.
5. A job longer than a fix becomes a new draft pull request: a branch with one empty commit,
   based on the open pull request that shares its files, with a description that states the
   problem, the evidence, what done means, and what is out of scope. That description is the
   next worker's whole task. It is opened without the label `workflow`: the tick starts a
   worker for every labelled draft, and adds the label itself when the ticket's turn comes
   (see "For the orchestrator session"). The session that opened it never adds the label
   `workflow`. A ticket that names its files under a `Files` heading, one path a line, is
   placed by that list instead of a model's guess. It gives the ticket a priority
   label (see the merge gate): `priority: high` when the ticket removes a bottleneck of the
   workflow or CI, otherwise `priority: medium`, and never `priority: blocker` or
   `priority: low`, which are the owner's and the orchestrator's.
6. When nothing is left open, add the label `audited`.
7. Use the merge gate to merge, if all four hold: the label `audited` is set; the checks on
  GitHub pass; the base is `main`; no question to the owner is open. Merge as one squashed commit.
8. After a merge, for each pull request that was built on this one: set its base to `main` and
   merge `main` into its branch. A conflict that is not mechanical is asked.
9. Name each ticket pull request this audit opened, by its number and title, and say that it
   waits for its start by the tick. Then say "audit finished" on a line of its own, as the last
   line, and stop. The tick ends the session when it reads that line.

A pull request whose base is not `main` is audited and labelled but not merged. It merges
after the one under it has merged and its own checks pass; the orchestrator starts a short
audit session for it then.

## Analyse the workflow

The analyst is the session `analyst-<name>`. The tick starts one a day, from 08:00 UTC once the
day's metrics report is written, as `analyst-day-<date>` over the last day, and on Sunday as
`analyst-week-<date>` over the last 7 days, when the 5-hour usage window allows a start (not
above 85% unless it resets within the hour). By hand it is
`shipline session start analyst <name> [1-7]`, the number being the days it reads. Its
sentence is "Analyse the workflow over the last day (or the last N days), as analyst-<name>, by
the section Analyse the workflow of AGENTS.md in this worktree". It runs Claude Opus at medium
effort in a copy of `main` on no branch, and has 40 minutes: the tick stops it then, whatever
it is doing.

1. It is read-only. It opens no ticket and no pull request, commits and pushes nothing,
   changes no label, comments nowhere, starts, stops and types into no session, and asks the
   owner nothing. The one thing it writes is its report folder, `$WORKFLOW_ANALYSIS_REPORT`:
   `.local/analyses/<name>/` in the main checkout. Its permission prompts reach nobody: what it
   cannot read without asking, it leaves out and names in the report.
2. It reads, for the days its sentence names: the day's metrics report,
   `.local/analyses/metrics-<date>.md` in the main checkout, and last week's for comparison;
   the wake log, `~/.local/state/workflow/wake.log`, and the metrics log beside it; the tick's
   log (`journalctl --user -u workflow-tick`); the gate's refusals and hand-backs and the
   tick's notes in `.local/workflow/` of the main checkout; the sessions' closing messages, as
   their pull requests' comments and reviews show them (`gh`, reading only); and the earlier
   analyses. It computes no numbers of its own: every number it gives is the metrics report's,
   and every other claim points to the lines that show it.
3. It looks for gaps of these kinds, with the evidence for each:
   - speed: where a pull request's time goes, and what changed since last week;
   - a model doing a job with one right answer: a decision made by hand every day, a wait a
     script could check, a nudge with a fixed text;
   - something that depends on a session's memory and is lost on a restart or a compaction;
   - a rule in this file that sessions do not follow, or follow at a cost;
   - a failure that repeated: the same lost check, the same flaky test, the same refused push;
   - a question to the owner that a rule or a standing decision already answers;
   - tokens or machine time spent on something that bought no merge.
4. It proposes at most three changes, fewer or none when the evidence holds no more. Each has
   its evidence, its predicted gain, in the metrics report's numbers, and the quality signal
   that would show its cost (a number of the report's quality part, such as sent-back reviews,
   red runs on `main`, hunt findings or weaker tests). A proposal that raises throughput by
   running fewer tests, skipping a job, shortening a review or widening a skip rule is refused:
   quality signals are a constraint, not a trade.
5. It writes `report.md` in its report folder. Its first section is headed `## Proposals`; each
   proposal there is numbered `1.`, `2.`, `3.`, and the first line of each says the change in
   one sentence, as the wake carries only those lines. Then come the gaps it found with their
   evidence, and what it read and found no gap in.
6. Within 35 minutes it publishes the report as a document, writes the link alone on the one
   line of `report.url` in the same folder, and then says `analysis finished` on a line of its
   own, as the last line. The tick stops it on that line.

## For the orchestrator session

When it starts, and again after a restart, the orchestrator writes its session name as listed by `claude agents` to `.local/workflow/orchestrator` in the main checkout (one line).

Workers are started by a script, build and mark ready. Fresh reviewers review and merge. Audits are started by the
orchestrator, by hand, and only for the pull requests built before the workflow existed.

`shipline tick` has no model in it (beyond the one that carries the wake's message, below)
and is run every 3 minutes by a systemd user timer
(`shipline install-timer` switches it on). It does only what has one right answer: it
stops an idle audit whose screen has the line "audit finished", a reviewer with
"review finished" (or "review sent back" once its pull request is draft), and an idle worker
with "worker finished" (unless its pull request is still a draft), runs
`session cleanup`, squeezes an idle session that holds more than 300 MB, gives the next
waiting draft ticket the label `workflow` when its turn has come (below), and starts a worker
for each draft pull request labelled `workflow` that has no session. A session that the usage
limit stopped is not working and not waiting for the owner: `session list` shows it as
`limit`, with the limit and the time it resets. That is Claude's own line ("You've hit your
session limit" or "You've hit your weekly limit"), or a closing message of the session whose
first line names the usage limit, says it was reached and says the session is stopping, when
the usage is at 90% or more; the
stop hook then notes the reset and the line reads `usage limit  resets <day, hour> (UTC)`.
For Claude's weekly limit, or another limit whose hour has no day, the usage figures supply
the day when their reset agrees with the printed hour within one hour. The weekly line uses
the weekly window; other names use a full window (the weekly one when both are full). The
stop hook saves it, or `session list` does when Claude stopped without that hook. A new stop
whose usage figures still name a reset already passed retries ten minutes on, with the
existing grace minute. The tick
squeezes it as it does an idle one and, when that time has passed, types the instruction of a
Claude worker, reviewer or audit into it again, once for each reset. Ready labelled pull
requests get a fresh `review-N` once the worker
has finished and stopped. Sent-back drafts get a worker that reads the independent review.
Codex responses reach the owner through their Haiku bridges; the tick reads completion
from the bridge and stops it together with Codex. It never starts an audit,
and nothing waits for one. It never starts a hunt either. It stops an idle hunt whose screen
has the line "hunt finished" and any hunt that still runs 65 minutes after its start, and
`session cleanup` then removes the hunt's worktree; the report stays. It starts the daily
analyst (see "Analyse the workflow"), stops it on the line "analysis finished", idle or not,
or 40 minutes after its start. `session cleanup` notes its report for the wake, however the
analyst ended, and removes its worktree; the wake tells the orchestrator once, at any hour and
busy or not. A daily start that failed is not tried again that day. Each hunt is stopped and
removed on its own: ending one never touches another's worktree, notes or report.

Every reviewer hands its passed review to the tick with `merge-when-current --handoff N`,
then ends with `review finished` and is stopped as usual. When the tick stops a reviewer
that ended with `review finished` while its pull request is still open, ready and based on
`main`, with neither a merge note nor a hand-back, it notes a rule break, and the wake tells
the orchestrator `rule break: review-N said review finished without a merge handoff` on a
line of its own, at any hour and busy or not, once. The pull request then gets a fresh
reviewer as any ready one without a session does.
The tick drives those notes without a model, calling the same gate from the kept worktree
only at the front of the train. Expected waits keep the note; a gate refresh records its
own pushed head. After a merge the tick records the stacked children, changes their bases
to `main` and merges `main` into each clean branch worktree and pushes. It keeps the parent
note until that work finishes, so a restart can continue after a base was changed.
A conflict first gets a mechanical refresh in the kept worktree by the repository's configured
conflict command, which resolves only the conflicts it recognizes and checks its own result
before the gate commits and pushes; success advances the note and keeps its train place. A
repository that configures none has no recognized conflict.
A failed check, unrecognized conflict, unrelated head change or gate refusal needing judgment drops the
note, revokes batch approval and requests one fresh reviewer. A child with its own session
is left to that session; an unlabelled child keeps its orchestrator-managed audit path.
A handed-off pull request whose checks failed is first read: the latest CI, image and E2E runs
of its head (it waits while one still runs), and for each failed, timed-out or cancelled job
(not the jobs whose name ends in "result") its failed log and its annotations. The first
failing test is the first pytest line `FAILED <file>.py::<test>` (or `ERROR`), else the first
Vitest `FAIL <file>.test.<ext> > <name>`, else the first Playwright
`1) [project] › <file>.spec.ts:L:C › <name>`; with none, it is "not reported". A job failed
for its runners when it timed out, or when it names no test and its log or annotations say
that the runner was lost, disconnected or shut down, lost communication or could not be
contacted, that the job exceeded its time, or "Failed to download action" or "Internal server
error".
- A job that failed on a test of its own: the tick makes the pull request a draft, revokes its
  handoff, keeps its place in the line as a blocked member, and starts a worker at medium
  effort whose sentence names the job, its run and the first failing test (below). The wake
  names it once as a hand-back. Its fix gets a fresh reviewer as any ready pull request does.
- Only the runners failed it: each failed run is re-run once, its failed jobs only, under the
  rules of main's retry (the run completed at its first attempt, every job completed, its
  failed jobs ran on the repository's own runners, no planner failed). The handoff waits for
  the re-run. A run that may not be re-run, or fails again, goes to one fresh reviewer.
- Anything else goes to one fresh reviewer, as before: a cancelled job, a failed job with no
  log or annotation to read, a failure no run of the head shows, and a second failing test in
  a row, one on the head the worker's fix made before a handed-off head of it passed.
A recorded hand-back preserves local work at its recorded head, unresolved merge conflicts
and local refresh commits after a failed push for its reviewer to inspect and finish
pushing and checking. It does not
discard local work or authorize a merge. The wake names each hand-back once, retaining it
until delivery.
Ready pull requests with a merge note do not get another reviewer just for waiting in line.

A reviewer still waiting for checks runs `shipline notify waiting` and ends its turn,
without asking the owner for "go on". Its check-wait note makes it eligible for one nudge
when all checks on the current head have finished, even before it publishes a round. A
reviewer or audit waiting in the merge train is nudged only when its pull request is first
and its checks have finished, once for each turn of the line, state of main and head; a
second pull request with green checks waits for its turn without a nudge. Without
that note, an idle reviewer still needs a round published after its session started, on the
current head or a commit the head grew from. The tick records the head under `.local/workflow/`
and logs each nudge in `~/.local/state/workflow/wake.log`. It leaves owner-blocked sessions
and unsent input alone; the reviewer reads the results and follows the merge gate rules.

The tick also keeps waits moving, for Claude workers and reviewers only (never Codex bridges,
hunts or audits), and never answers a question. It never interrupts a running command but in
two cases, a still screen and a wait loop that finds itself (below); the full table is in
`docs/nudge-rules.md`:

- A session idle 50 minutes or more that waits for its own checks or job (its `notify waiting`
  stop) or, as a reviewer, for its turn in the merge train gets one status message, once per
  idle stretch: "Status check: if what you were waiting for is done, carry on; if not, run
  `shipline notify waiting` and end your turn." Its cached context lasts an hour after
  its last turn, so the answer is cheap. Never one waiting for the owner, a finished one, one
  stopped on the usage limit, or one with unsent text in its box.
- A worker idle 15 minutes on a pull request that has been ready as long, without
  `worker finished`, is told once to finish by its rules. Its stop on a ready pull request
  rings the phone either way, so only the words of its last reply tell a question apart: a
  reply that asks the owner is left alone.
- A busy session whose screen (without the input box, the status line, the spinner and its
  clock) has not changed for 45 minutes is nudged once per stretch and head: the tick presses
  Escape, checks that the box is empty, and types "The command has produced nothing for 45
  minutes. Check whether it is still running and go on." It is named in the wake as
  `stuck? ticket-N (45 min, running: <first line of the running command>); nudge sent`.
  `session list` shows when each screen last changed.
- A busy session whose running wait loop has a `pgrep -f` or `ps | grep` pattern that matches
  its own shell, which never ends, is nudged once per loop the same way, with "That loop finds
  its own command line and never ends. Check the process by its number." It is named as
  `wait loop finds itself? ticket-N (running: ...); nudge sent`.
- Neither nudge goes to a session with text in its box, a message queued under its spinner
  (a `❯ ` line between the spinner and the box) or a question to the owner on its screen: it
  is named without `; nudge sent`. A permission prompt waits for the owner and is not called
  stuck. Should Escape still put a message back into the box, nothing is typed after it, and
  the line ends `; interrupted, text left in its box`. A wait loop is nudged only when it is
  the command started last and the screen has stood still for 5 minutes; while the screen
  moves, it is named when it stands still, the turn ends or 15 minutes have passed. A session
  stopped on the usage limit, a hunt, an audit, an analyst and a Codex bridge are left alone.
  A nudge whose Enter was not taken ends its line with `; nudge failed`, and is sent again at
  the next passes, without another Escape, while the box holds exactly it, three times at
  most; then the wake is told `stuck? ticket-N (nudge not taken; its text stays in the box)`.
- An idle session that none of these rules covers for 50 minutes is named in the wake once per
  head, as `idle, no rule: ticket-N (<min> min, last line: <line>)`. The tick types nothing it
  has no rule for; the orchestrator decides.

Each message and line is logged in `~/.local/state/workflow/wake.log`.

The orchestrator session does what needs judgment:

- Every decision of these kinds goes through `shipline decide`, never as chat alone: a start
  (`shipline decide start N`), a hold of a ticket that shares files with a running one
  (`shipline decide hold N --after M`, or `shipline decide start N --after M`), an answer to a
  worker under the owner's standing delegation (`shipline decide answer N --area <area>
  --question ... --reply ... --meaning ...`), the allowance of another review round after two
  sent back (`shipline decide allowance N --round R`), a hand-back for judgment and a restart
  of a session (`shipline decide hand-back N` or `shipline decide restart N`, with `--session`,
  `--judgment` and `--message`), and the program of a ticket's next worker
  (`shipline decide program N claude|codex|cloud`). The script checks each against these
  rules, records it in `.local/workflow/decisions.jsonl` of the main checkout, a refusal too,
  and does the act itself: the label, the `start-plan` line, the entry under `Answers given
  for the owner`, `review-N.allow-round`, the `Worker:` line and `worker-program-N`. When the
  decision is the owner's own word, it carries his exact words (`--owner "<his words>"`): an
  answer in money, privacy, legal text or text sent to a model, an allowance, and a start of a
  ticket marked `Start: owner only` need them. His word on a start also lifts the order and
  overlap checks, never the 5-hour window. A message to a session that carries a decision
  quotes the record's line that `shipline decide` printed. An interrupted decision is finished
  by the next call of `shipline decide`; the tick starts nothing for a ticket whose decision is
  unfinished.

- Every ticket it opens gets a priority label (see the merge gate): `priority: blocker` for
  what stops every merge or push now, `priority: high` for a bottleneck, otherwise
  `priority: medium`, and `priority: low` only on the owner's word. What it starts by hand
  it starts in the order the merge train serves: by priority, then the older ticket. The tick
  does the same with its own starts, and a start of the tick that waits for memory keeps the
  ones after it waiting too. The wake's check-in lists the
  ready pull requests grouped by priority.
- It starts no audit for a pull request labelled `workflow`. Of the pull requests built before
  the workflow existed (those without the label), it reads which are ready and not `audited`,
  groups them into stacks by their bases, and starts `shipline session start audit N`
  for the lowest one not yet audited of each stack. How many run at once is its decision, made
  from `session list` and the memory figures.
- It chooses the effort level when it starts a session by hand, as the third word after the
  number: `session start <worker|audit|review> N <level>`. `low` for a mechanical change (a rename, a
  one-line fix); `medium` for work whose description is complete and testable; `high` for work
  that touches stored data, text sent to a model, concurrency, or a design the description
  leaves open. Audits run at `high`. Without a level a worker gets `medium`, an audit `high`
  and an independent reviewer `medium`, or `high` for the paths `review.high_effort_paths`
  names, for Shipline's own concurrent scripts and for a description with the line
  `Review: high` (`review-effort N`); a level given by hand wins, and a conflict resolution's
  reviewer runs at `high`. The tick starts workers and reviewers with no level. When it opens a
  ticket whose review needs `high` that no path shows (text sent to a model, say), it writes
  `Review: high` on a line of its own in the description.
- It may choose the model as the word after the level: `opus` or `sonnet`. The default model is
  Opus. `sonnet` is used only when all of these hold: the change is mechanical; the description
  names exactly what changes; existing tests would catch a mistake; it does not touch stored
  data, text sent to a model, or anything security related. An audit or independent reviewer is never run on a smaller
  model; the script starts every audit on Opus and refuses another. A cheaper model is not a
  saving when its result costs more to repair: if a `sonnet` worker's own review comes back
  with more than two findings, or two review rounds each find something, stop it and start
  the pull request again on Opus rather than patching. The tick uses the worker program setting;
  independent reviewers always use Claude Opus.
- A bug fix, screens included, may go to Codex. When the orchestrator opens or starts a ticket
  whose task fixes a bug and invents no new UI (no new control, layout or wording to choose),
  it records `shipline decide program N codex`, which writes `Worker: Codex (a bug fix with no new UI)`
  on a line of its own in the description. A ticket whose task needs clickable options never gets that line and goes to
  Claude. Without the line the tick routes as before.
- It asks each audit session to tell it when it goes idle (`SendMessage` with
  `notify_when_idle`), so it reacts to a finished audit without polling.
- When an audit ends, it starts the next one in that stack. When an audit merged its pull
  request, it tells the audits still running that `main` moved. The message names what merged
  and which of the receiving branch's files it touched, and asks for a merge of `main` only
  when it touched one of them or the branch now has a conflict; otherwise it says that no merge
  is needed. Its messages to workers after a merge follow the same rule: never "merge `main`
  before your last review round" or "if GitHub asks for it".
- A pull request that is `audited`, based on `main` after the one beneath it merged, with
  passing checks, is merged by the orchestrator asking a short audit session to do steps 7
  and 8. The orchestrator itself never merges and never writes code.
- A pull request labelled `workflow` that is ready, based on `main` and has no session was
  left behind: an audit merged the one beneath it, or its worker was stopped. The orchestrator
  starts its independent reviewer with `session start review N`, which reviews and merges.
  A previously reviewed stack member is also restarted this way to finish its merge.
- A session started by the script reads this file from `main`, whatever its branch holds, so
  a branch made before a rule changed needs nothing from the orchestrator. Only a session that
  was started before its sentence named `main`, or one started by hand, may still follow the
  copy in its worktree: it rings the phone for a progress note, or marks its pull request
  ready and stays idle without saying "worker finished". The orchestrator tells such a session
  (`SendMessage`) to read `git show origin/main:AGENTS.md` after a fetch and finish by it.
- It starts a hunt when the owner asks for one:
  `shipline session start hunt <name> [plan|explore] [claude|codex|cursor] [<area>]`,
  the name saying what the hunt is (`plan-phone-2`). The area, one of `hunts.areas`, is the
  last word, after the kind and the program. A hunt without an area hunts every screen and
  runs alone. A second hunt starts only beside a running hunt that has an area, and must name
  a different area; a third is refused, and with no areas configured one hunt runs at a time.
  Each runs a browser and a copy of the application, and two hunts over the same screens would
  report the same bugs twice. The copy is run by the repository's configured hunt command,
  and a Cursor hunter is its configured one too; without them the script refuses the hunt in a
  sentence. To compare hunter programs it runs the same kind of hunt once
  with each, one after the other.
- When a check-in carries an analysis (`Workflow analysis day-<date> (report <link>): 1. ...`),
  it opens a draft ticket for each proposal it judges worth it, as in step 5 of the audit
  rules, naming the report, and gives the owner the link and the tickets it opened. The owner
  can say which to drop; a dropped ticket's pull request it closes. An analysis that ended
  without publishing its report it names to the owner, with its folder.
- When a hunt has ended, it reads the report in `.local/hunts/<name>` of the main checkout.
  A Codex or Cursor hunter's report it publishes as a document with the pictures; a Claude Code hunter
  published its own (`report.url`). It gives the owner the link and the count of findings.
  Which findings become tickets is the owner's decision; for each he names, it opens the
  draft pull request as in step 5 of the audit rules.
- Starting a draft ticket is the tick's job (`scripts/workflow/ticket_starts.py`). A draft
  pull request without the label `workflow` is a ticket that waits for its turn, whoever opened
  it: a worker, an audit, or the orchestrator for a finding of a hunt. Each tick takes those
  drafts, leaving out a cloud worker's (a `cloud-N` record, or the line `Worker: cloud` on
  its own in the description) and one only the owner starts (the line `Start: owner only`),
  and orders them by priority, then the older ticket. The first one goes whose files overlap
  no running ticket's (an open pull request labelled `workflow`, one with a session or a
  cloud record, one in the merge train, except a ready train member with no session or cloud
  record: a pull request only waiting in the merge train is not running), that is based on
  `main`, and that no line of
  `.local/workflow/start-plan` in the main checkout holds back, so tickets that touch the same
  files run one after another. It gets the label `workflow`,
  and the worker starts as for any labelled draft. At most one a tick, and only when memory
  and the load have been under the ceilings of `session start` for 10 minutes, nothing started
  in the last 5, and the 5-hour usage window allows starts (as in the check-in rule below).
- The files of a ticket are in `.local/workflow/files-N` of the main checkout, one path a
  line; `shipline ticket-files N` prints them. They are the diff's when the ticket
  has a real diff, the list under a `Files` heading of its description when it has one, and
  otherwise the answer of one short `claude -p` on Claude Haiku 5.5 that read the description,
  asked once per ticket and again only when the description changes.
- `start-plan` holds what the files do not show, one rule a line: `609 after 605` (609 waits
  while 605 is open), `hold 590` (590 does not start), `hold all` (nothing starts by itself).
  The orchestrator writes those lines when the owner gives it a rule, and nothing else per
  ticket; a hold of one ticket after another is `shipline decide hold`, which writes its own
  line and removes it when the hold ends. The orchestrator still names the waiting tickets to the owner
  when asked, from `gh pr list --draft --json number,title,labels`, and starts one at once
  when he says so, with `shipline decide start N --owner "<his words>"`.
  The wake's check-in tells it what started and why the next one waits
  (`608 waits: shares scripts/workflow/merge_gate.py with 563`), a line each, once per change,
  and from the decision records each held ticket with its reason and age, and the answers
  given and starts refused since the last check-in.
- A session listed as `limit` is stopped on the usage limit. It is reported as that, never as
  working. The tick sends it its instruction again after the session or weekly reset, and
  the wake tells the orchestrator which sessions were resent. Four stay as they are
  until the orchestrator acts: one whose line names no time, one stopped by another limit
  than the session limit whose line names an hour and no day when the usage figures cannot
  be read or disagree with that hour, one with a text in its box
  that was not sent, and a hunt (`session resend <name>` types the instruction again,
  `session stop <name>` ends it).
- It is woken from outside its own session by `shipline wake`, which the tick runs: a
  schedule kept inside the orchestrator stops with it on the usage limit. Right after the tick
  sent sessions their instruction again after a reset, the wake tells the orchestrator which,
  at any hour; a message that could not be sent is tried again every 5 minutes until it is. Every 20 minutes from 20:00 to 8:00 it looks again and gives the orchestrator a
  check-in when it is idle and a session, a pull request or its checks, or whether it may
  start sessions changed since the last one. When `/tmp` has used more than 80% of its file
  slots, it tells the orchestrator once, at any hour and busy or not. A session whose
  stop rang the phone (see "Phone pushes") is named on a line of its own with how long it has
  waited, `asking the owner: review-156 (8 min)`, until he answers; a new question reaches the
  orchestrator once, at any hour and busy or not. A `stuck?`, `wait loop finds itself?` or
  `idle, no rule:` line of the tick reaches it the same way, once, and so does `owner question
  without the Owner only line: ticket-426` (or `without a reply word`). A question the tick
  answered for the owner (see "Asking the owner") is named `answered for the owner: ticket-426,
  "1" (03:12)`: one answered at night in the first check-in from 8:00, which lists every answer
  since 20:00, one answered in the day at the next look. The orchestrator passes them on to the
  owner, who may overrule them. The
  check-in names the usage, the `/tmp` file slots, the sessions and the open pull requests; it is delivered as a
  message from a short `claude -p` on Claude Haiku 5.5. The wake itself starts,
  merges and pushes nothing. What it saw and did is in `~/.local/state/workflow/wake.log`. A
  check-in is answered by the rules of this section: nothing is started above 85% of the
  5-hour window unless it resets within the hour.
- It answers "what is everyone doing" for the owner. A pull request handed to the tick, or
  an audit that waits in the merge train, is waiting for its turn, not stuck:
  `merge-when-current --train-status` names the first and the order. A reviewer that handed
  off is neither waiting for the owner nor idle in the line; the tick stops it.
- A session that ended its turn only to wait for its own background job (a Codex run, a test
  run) is not waiting for the owner. It is not treated as blocked or idle on that ground alone.

This runs only while the orchestrator session is open. With it closed, workers and independent reviewers still start,
finish and merge; only the pull requests built before the workflow existed wait for their
audit until it is opened again.

## Phone pushes

The owner's phone rings when a worker, reviewer or audit is blocked on him, and only then.
`shipline notify` decides, called by four hooks that `shipline install-hooks`
adds to `~/.claude/settings.json` (the owner runs it once).

- A worker that ends its turn rings the phone unless the last line of its last message is
  "worker finished". While its pull request is a draft it rings only when the last message
  asks the owner something.
- A reviewer that ends its turn rings unless its last line is `review finished`, or
  `review sent back` and GitHub confirms the pull request is draft. A failed send-back
  still rings.
- A Codex worker relays through its Haiku bridge, whose ordinary hooks ring with the
  question and Remote Control link. Codex notify only records the turn for that bridge;
  callbacks from reviews and hunts do nothing. Neither usage limits nor awaited jobs ring.
- An audit that ends its turn rings the phone unless the last line of its last message is
  "audit finished".
- A hunt that ends its turn rings the phone unless the last line of its last message is
  "hunt finished". A hunt asks the owner nothing, so a ring means it could not go on. A Codex or Cursor
  hunter has no hooks and never rings.
- An analyst never rings, not even on a permission prompt: it asks nobody anything, and the
  tick reads its closing line and stops it on its time.
- A Claude worker, reviewer or audit that stops on the usage limit, in its own words (see "For the
  orchestrator session"), does not ring: it waits for the reset, not for the owner.
- Claude's own weekly-limit line also waits for the reset without ringing when the usage
  figures supply its day and agree with its hour within one hour. Other hour-only limits
  use a full usage window under the same check. The tick resends after the saved reset once,
  and the wake tells the orchestrator; unreadable or disagreeing figures leave it waiting
  for the orchestrator as before. Hunts are never resent.
- A permission prompt always rings, except an analyst's.
- A question to the owner is plain text at the end of a turn (see "Asking the owner"), so it
  rings as that stop does. The question tool is not used; its hook stays, so that a session
  which used it anyway still rings.
- Before ending a turn only to wait for your own background job (a Codex run, a test run), run
  `shipline notify waiting`. That one stop then neither rings nor marks the session
  idle. Do not run it when the turn also asks the owner something.
  A reviewer is then marked as waiting for checks, so the tick can wake it when they
  finish; it is not idle, so its running job is not squeezed. It does not ring the phone.
  The stop also notes when the wait began: after 50 minutes the tick asks a Claude worker or
  reviewer once whether the wait is over (see "For the orchestrator session"). Typing that
  message rings nothing; the stop after it rings as any stop does.
- A push that could not be sent is written to `~/.local/state/workflow/missed-pushes.log`.
