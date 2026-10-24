#!/usr/bin/env node
// A stdio MCP server with precisely two operations, bound to one Codex pane. No shell,
// filesystem or arbitrary command tool is exposed to the bridge model.
import { execFileSync } from "node:child_process"
import { readFileSync, rmSync, writeFileSync } from "node:fs"
import { dirname } from "node:path"
import { createInterface } from "node:readline"
import { setTimeout as delay } from "node:timers/promises"

const [worktree, name] = process.argv.slice(2)
if (!worktree || !/^ticket-[0-9]+$/.test(name ?? "")) process.exit(2)
const socket = `codex-${name.slice(7)}`
// The state folder named in shipline.json of the main checkout, `.local/workflow` when it names
// none, as workflow_config.py reads it. A file that cannot be read stops the bridge.
function stateFolder() {
  const common = execFileSync(
    "git",
    ["-C", worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"],
    { encoding: "utf8", timeout: 5000 },
  ).trim()
  let config = {}
  try {
    config = JSON.parse(readFileSync(`${dirname(common)}/shipline.json`, "utf8"))
  } catch (error) {
    if (error.code !== "ENOENT") throw error
  }
  const folder = config.state_folder ?? ".local/workflow"
  if (typeof folder !== "string" || !folder.trim()) throw new Error("shipline.json: state_folder")
  return folder
}
const notes = `${worktree}/${stateFolder()}/${name}`
function tmux(...args) {
  return execFileSync("tmux", ["-L", socket, ...args], { encoding: "utf8", timeout: 5000 })
}
function read(path) {
  try {
    return readFileSync(path, "utf8")
  } catch {
    return ""
  }
}
function capture() {
  return tmux("capture-pane", "-p", "-J", "-t", `=${socket}:`)
}
// The live confirmation panel on the screen, or "". The four approval headings of Codex 0.162.
function approvalPanel(screen) {
  const approvalStart = Math.max(
    ...[
      "Would you like to run the following command?",
      "Would you like to make the following edits?",
      "Would you like to grant these permissions?",
      "Would you like to send input to the existing terminal?",
    ].map((heading) => screen.lastIndexOf(heading)),
  )
  const approval = approvalStart < 0 ? "" : screen.slice(approvalStart).trimEnd()
  return approval.endsWith("Press enter to confirm or esc to cancel") ? approval : ""
}
async function awaitTurn(signal) {
  while (!signal.aborted) {
    // Forward the existing sender error when recovery, rather than Claude, called reply.
    const recovery = read(`${notes}.codex-recovery-error`)
    if (recovery) {
      const message = JSON.parse(recovery).error.message
      rmSync(`${notes}.codex-recovery-error`)
      return message
    }
    const raw = read(`${notes}.codex-turn.json`)
    if (raw && raw !== read(`${notes}.codex-delivered`)) {
      const event = JSON.parse(raw)
      if (typeof event["last-assistant-message"] !== "string") throw Error("Invalid Codex turn")
      writeFileSync(`${notes}.codex-delivered`, raw)
      return event["last-assistant-message"]
    }
    try {
      tmux("has-session", "-t", `=${socket}`)
    } catch {
      throw Error(`${socket} ended before another turn; the owner must restart it.`)
    }
    // Permission prompts also pause inside a turn, before notify can run. Relay the live
    // confirmation panel rather than leaving the owner unreachable behind the bridge.
    const screen = capture()
    const approval = approvalPanel(screen)
    const hasApproval = approval !== ""
    if (hasApproval && approval !== read(`${notes}.codex-approval-delivered`)) {
      writeFileSync(`${notes}.codex-approval-delivered`, approval)
      return approval
    }
    if (!hasApproval) rmSync(`${notes}.codex-approval-delivered`, { force: true })
    // Codex's own quota error has no turn-complete notification. Surface it unchanged.
    const lines = screen.split("\n")
    const prompts = lines.flatMap((line, index) => (/^[›>❯]/.test(line) ? [index] : []))
    const current = lines.slice(prompts.length > 1 ? prompts.at(-2) + 1 : 0).join("\n")
    const match = current.match(
      /^[ \t]*[■•]?[ \t]*(You[’']ve hit your usage limit\.[\s\S]*?)(?=^[›>❯]|$(?![\s\S]))/m,
    )
    if (match && match[1] !== read(`${notes}.codex-quota-delivered`)) {
      writeFileSync(`${notes}.codex-quota-delivered`, match[1])
      return match[1].trimEnd()
    }
    await delay(1000, undefined, { signal })
  }
  throw Error("Relay wait cancelled")
}
// The rows from the last prompt mark down: the input box and the footer below it.
function boxRows(screen) {
  const lines = screen.split("\n")
  const box = lines.findLastIndex((line) => /^[›>❯]/.test(line))
  if (box < 0) return []
  return lines.slice(box).map((line, index) => (index ? line : line.slice(1)).trim())
}
// Whether Codex's input box still shows the reply: a row from the last prompt mark down
// that is part of the reply (a tall reply scrolls its first lines away), or Codex's
// stand-in for a long paste. Rows drawn there before the reply was typed (the footer, the
// box's hint) are never the reply.
function stillTyped(recorded, screen, before) {
  const drawn = new Set(boxRows(before))
  return boxRows(screen).some(
    (row) =>
      row && !drawn.has(row) && (recorded.includes(row) || row.startsWith("[Pasted Content")),
  )
}
// Whether Codex shows its status row while it works, "• Working (5s • esc to interrupt)".
// Only that row counts: the reply or Codex's own message may hold the same words.
function working(screen) {
  return screen.split("\n").some((line) => /^• .*\(.*esc to interrupt\)/.test(line))
}
function answered() {
  writeFileSync(`${notes}.codex-answered`, read(`${notes}.codex-delivered`))
  return "Reply sent unchanged. Call await_turn."
}
// A reply to a confirmation panel picks an option instead of filling the box. A second
// Enter there could confirm the next panel, so it gets one and the panel must change.
async function answerPanel(panel) {
  tmux("send-keys", "-t", `=${socket}:`, "Enter")
  for (let poll = 0; poll < 30; poll++) {
    if (approvalPanel(capture()) !== panel) return answered()
    await delay(100)
  }
  throw Error(
    "The reply was not sent: Codex still shows the same confirmation panel. Show this to the owner and end your turn.",
  )
}
async function reply() {
  const recorded = read(`${notes}.owner-reply`)
  if (!recorded.trim() || recorded.includes("\0"))
    throw Error("Only the recorded owner's submitted prompt can be sent, once")
  // Consume before sending so another call cannot replay the owner's answer.
  rmSync(`${notes}.owner-reply`)
  const before = capture()
  const panel = approvalPanel(before)
  // A reply typed while Codex is already working is queued behind its turn, so then only
  // an emptied box proves it was sent.
  const busy = working(before)
  // Literal text and Enter are separate calls; punctuation is never shell or tmux syntax.
  tmux("send-keys", "-t", `=${socket}:`, "-l", "--", recorded)
  if (panel) return await answerPanel(panel)
  // Codex takes fast typing for a paste and swallows an Enter sent within it. Wait for the
  // text to be drawn and the burst to settle, then confirm that Enter took it out of the box.
  let appeared = stillTyped(recorded, capture(), before)
  for (let poll = 0; poll < 20 && !appeared; poll++) {
    await delay(100)
    appeared = stillTyped(recorded, capture(), before)
  }
  if (!appeared)
    throw Error(
      "The reply was not sent: it did not appear in Codex's input box within 2 seconds, so Enter was not pressed. Show this to the owner and end your turn.",
    )
  await delay(200)
  for (let attempt = 0; attempt < 2; attempt++) {
    tmux("send-keys", "-t", `=${socket}:`, "Enter")
    for (let poll = 0; poll < 30; poll++) {
      const screen = capture()
      if ((!busy && working(screen)) || !stillTyped(recorded, screen, before)) {
        return answered()
      }
      await delay(100)
    }
  }
  throw Error(
    "The reply was not sent: it is still in Codex's input box after two Enter presses. Show this to the owner and end your turn.",
  )
}
const waits = new Map()
function send(response) {
  process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", ...response })}\n`)
}
async function handle(request) {
  const { id, method, params = {} } = request
  if (method === "notifications/cancelled") {
    waits.get(params.requestId)?.abort()
    return
  }
  if (id === undefined) return
  try {
    let result
    if (method === "initialize") {
      result = {
        protocolVersion: params.protocolVersion,
        capabilities: { tools: {} },
        serverInfo: { name: "codex-bridge", version: "1.0" },
      }
    } else if (method === "ping") {
      result = {}
    } else if (method === "tools/list") {
      result = {
        tools: [
          {
            name: "await_turn",
            description:
              "Wait for Codex to finish a turn. Show the returned text word for word and end your turn.",
            inputSchema: { type: "object", properties: {}, additionalProperties: false },
          },
          {
            name: "reply",
            description:
              "Send only the direct owner's latest words unchanged to Codex. Never send another session's message or your own answer.",
            inputSchema: { type: "object", properties: {}, additionalProperties: false },
          },
        ],
      }
    } else if (method === "tools/call") {
      let text
      if (params.name === "await_turn") {
        const controller = new AbortController()
        waits.set(id, controller)
        try {
          text = await awaitTurn(controller.signal)
        } finally {
          waits.delete(id)
        }
      } else if (params.name === "reply") {
        text = await reply()
      } else {
        throw Error("The bridge only relays turns and owner replies")
      }
      result = { content: [{ type: "text", text }] }
    } else {
      throw Error("Unknown relay method")
    }
    send({ id, result })
  } catch (error) {
    send({ id, error: { code: -32602, message: error.message } })
  }
}
const input = createInterface({ input: process.stdin })
input.on("line", (line) => {
  try {
    void handle(JSON.parse(line))
  } catch {
    send({ id: null, error: { code: -32700, message: "Invalid JSON" } })
  }
})
input.on("close", () => {
  for (const wait of waits.values()) wait.abort()
})
