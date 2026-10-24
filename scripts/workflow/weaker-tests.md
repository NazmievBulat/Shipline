# Reviewing changes to tests

From the repository root:

```bash
scripts/workflow/weaker-tests --base origin/main
scripts/workflow/weaker-tests --base origin/main --working-tree
scripts/workflow/weaker-tests 227
```

The first command compares the merge base with HEAD. `--head <commit>` selects a published
head; `--working-tree` also includes tracked staged/unstaged changes. A PR number looks up its
base/head and fetches them. `--json` emits the same items as structured records. Findings have
exit status zero; an unreadable Git/GitHub input is an operational error, not a clean report.

The panel writes `weaker-tests.md` beside its reports and copies it to each `.review/` folder.
Workers explain their items in the PR description. Independent reviewers explain every item
in the published review: why it is justified and how the replacement still catches the original
bug, preferably with a deliberate-break run. Missing explanations are findings. The
workflow rules (RULES.md) own this rule.

The script uses Python ASTs for assertions/test definitions, a small JS tokenizer for multiline
expect chains, named page-object verification calls and test declarations, and numeric comparisons of matching expressions. It also
reads test helpers and Playwright/Vitest config files. Strings/comments are excluded from
marker and numeric matching, so fixture programs do not become findings.

A Storybook story with a `play` function is a test too: a story test runner runs each one, so
its assertions and its removal are reported like any other. A browser check that moved to a
story keeps its trace: the story carries `// Moved from <spec>: "<title>"` on the line above
it, and the spec's deleted test is then reported as **test moved to a story**, naming the
story. It is still an item to explain, with the run that shows the story failing on a broken
component. A helper that several stories' `play` functions share is a `*.play.ts` file, and
its assertions are read as a story's are.

This is a heuristic aid to reviewing the full diff. It does not prove arbitrary assertion
changes equivalent, track aliases/data flow, interpret computed bounds or perform mutation
testing. Substantially rewritten tests can appear as deletions. A deleted test file is one
item; its assertions and tests are not listed again. Exact matchers replaced by partial ones
(`toMatchObject`, `expect.objectContaining`, `toHaveBeenCalled` for `toHaveBeenCalledTimes`,
`toEqual` for `toStrictEqual`) count as weakened. An assertion-count drop and
its individual removals may share one explanation. Deleted locations refer to the old file;
other locations refer to the new file.
