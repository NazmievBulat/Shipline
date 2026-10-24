#!/usr/bin/env node
// Be conservative: an unreadable or ambiguous task stays with Claude.
import { execFileSync } from "node:child_process"

const number = process.argv[2]
if (!/^\d+$/.test(number ?? "")) process.exit(1)
function gh(...args) {
  return execFileSync("gh", args, { encoding: "utf8", timeout: 30_000 })
}
function withoutTestIds(text) {
  return text.replace(/\s*data-testid\s*=\s*(?:"[^"]*"|'[^']*'|\{[^{}]*\})/g, "")
}
function onlyRouteList(text) {
  // A data-only route array. A route handler or other executable code is screen work.
  return /^(?:(?:export\s+)?const\s+\w+\s*=\s*)?\[\s*(?:["']\/[/\w:.*?#[\]-]*["']\s*,?\s*)*\]\s*;?$/.test(
    text.trim(),
  )
}
let read
function ticket() {
  read ??= JSON.parse(gh("pr", "view", number, "--json", "title,body,baseRefName,headRefOid"))
  return read
}
// The orchestrator writes this line on a bug fix whose task invents no new UI. It is read only
// for a ticket the change alone gives Claude, so a backend ticket needs no description.
function toClaude() {
  const marked = /^[ \t]*Worker: Codex \(a bug fix with no new UI\)[ \t]*$/m
  process.exit(marked.test(ticket().body ?? "") ? 0 : 1)
}
try {
  const patch = gh("pr", "diff", number, "--color", "never")
  if (patch.trim()) {
    const files = patch.split(/(?=^diff --git )/m).filter((file) => file.startsWith("diff --git "))
    if (!files.length) toClaude()
    for (const file of files) {
      const header = file.split("\n", 1)[0]
      if (!/(?:a|b)\/(?:web|ui)\//.test(header)) continue
      // Renames, binary files, modes and additions/deletions need Claude too.
      if (
        /^(?:rename |old mode|new mode|new file|deleted file|Binary files|GIT binary)/m.test(file)
      )
        toClaude()
      const removed = [],
        added = [],
        hunks = []
      for (const line of file.split("\n")) {
        if (line.startsWith("@@")) {
          hunks.push({ before: [], after: [] })
          continue
        }
        const hunk = hunks.at(-1)
        if (!hunk || line.startsWith("\\")) continue
        if (line.startsWith("-")) removed.push(line.slice(1))
        if (line.startsWith("+")) added.push(line.slice(1))
        // Each hunk is compared in order with its context, so a moved line counts as a change.
        if (!line.startsWith("+")) hunk.before.push(line.slice(1))
        if (!line.startsWith("-")) hunk.after.push(line.slice(1))
      }
      const before = removed.join("\n"),
        after = added.join("\n")
      if (removed.length + added.length === 0) toClaude()
      if (
        hunks.every(
          (hunk) =>
            withoutTestIds(hunk.before.join("\n")) === withoutTestIds(hunk.after.join("\n")),
        )
      )
        continue
      if (
        /\/(?:routes|route-list)\.[cm]?[jt]s\b/.test(header) &&
        onlyRouteList(before) &&
        onlyRouteList(after)
      )
        continue
      toClaude()
    }
  } else {
    const { title, body } = ticket()
    // Empty tickets have no code yet: explicit backend/scripts/tests scope is required.
    // Screen words in the title always count; the test-id exception is the whole body.
    const text = `${title ?? ""}\n${body ?? ""}`
    const exceptions =
      /^\s*(?:only|solely)\s+(?:(?:change|update|add|fix|rename)\s+)?(?:test[ -]?ids?|data-testid|(?:the\s+)?route list)(?:\s+(?:in|under|for)\s+[`\w./*-]+)?[.!]?\s*$/i.test(
        body ?? "",
      )
    const screen =
      /(?:\bweb\/|\bui\/)|\b(screens?|interfaces?|frontend|layouts?|buttons?|wording|pages?|styling|styles?|ui|dialogs?|modals?|forms?|menus?|components?|css|theme|icons?|panels?|tooltips?|toasts?|banners?|mobile|responsive|animations?)\b/i
    if (screen.test(title ?? "") || (screen.test(text) && !exceptions)) toClaude()
    // Tests alone say nothing of where the change is: nearly every ticket asks for them.
    if (!exceptions && !/\b(backend|security|scripts?|workflow|api|database)\b/i.test(text))
      toClaude()
  }
  process.exit(0)
} catch (error) {
  process.stderr.write(`Codex routing: ${error.message}\n`)
  process.exit(1)
}
