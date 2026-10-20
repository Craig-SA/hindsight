/**
 * The memory watcher's hook (`autoInject: "watch"`, see core/session-sync.ts), for every harness
 * with a PostToolUse hook that hands `additionalContext` to the model (Claude Code, Codex;
 * SESSION_HARNESSES in core/session-events.ts names each one's entry file and how to read its
 * transcript). A hook in two halves, so the agent never waits on memory:
 *
 *  - the hook itself (milliseconds): hand over a note an earlier sync left, as `additionalContext`
 *    next to this tool result, then start this step's sync in a detached background process;
 *  - the background process (`--work`): send the transcript's new lines to the session and stash
 *    the notes it returns (the watcher itself runs in Hindsight); the next tool call (or prompt, or
 *    Stop) hands them over. A server without sessions has no watcher: the hook stays silent.
 *
 * Claude Code's own background hooks were tried and do not fit: an `async` hook's context arrives
 * only with the user's next message (never mid-task in `claude -p`), and an `asyncRewake` one is
 * shown to the agent as "PostToolUse hook blocking error" — the framing under which an agent read a
 * real decision as an injection and refused it. A plain hook's `additionalContext` is the channel
 * the sde-bench watcher measured.
 */
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { deriveBankIdOrSkip } from "./bank";
import { applyBankConfig, loadConfig, watches, type Config } from "./config";
import { DAEMON_WAIT_RETAIN_MS, ensureDaemon } from "./daemon";
import { diag, readHookEvent } from "./diag";
import { HindsightClient } from "./hindsight";
import { describeError } from "./log";
import { sessionRootDir, stashPendingContext, takePendingContext } from "./session-cache";
import { SESSION_HARNESSES } from "./session-events";
import { sessionOptions, sessionsUnsupported, syncSession, type SyncResult } from "./session-sync";

const EVENT_ENV = "HINDSIGHT_WATCH_EVENT";

/** Where a session's sync state lives (the Stop hook reads it too). */
export const sessionStateDir = (sessionId: string): string =>
  join(tmpdir(), "hindsight-watch", sessionId.replace(/[^\w-]/g, "_"));

/** The pending-context slot the watcher's notes wait in until a hook hands them over. */
export const notesKey = (harness: string): string => `${harness}-watch`;

interface WatchEvent {
  session_id?: string;
  transcript_path?: string;
  cwd?: string;
  /** The prompt being submitted, when the prompt hook started this sync. */
  prompt?: string;
  /** The turn's final reply, when the Stop hook started this sync. */
  reply?: string;
}

/** Start a sync without waiting for it: the sync outlives the hook. The prompt hook starts one
 *  too, so the heads-up check starts while the agent reads its first files, not at its first tool
 *  call. */
export function startSync(harness: string, ev: WatchEvent): void {
  const entry = SESSION_HARNESSES[harness]?.entry;
  if (!entry || !ev.session_id) return;
  try {
    const self = join(dirname(fileURLToPath(import.meta.url)), entry);
    const child = spawn(process.execPath, [self, "--work"], {
      detached: true,
      stdio: "ignore",
      windowsHide: true,
      env: { ...process.env, [EVENT_ENV]: JSON.stringify(ev) },
    });
    child.on("error", () => {});
    child.unref();
  } catch {
    /* best-effort: a failed spawn must not touch the agent */
  }
}

/** The config this repo's session runs with, or null when it does not watch: the per-bank and
 *  per-path overrides apply, as in session start and the prompt hook, so a repo set to "reflect"
 *  (or outside `optInOnly`) gets neither the notes nor a detached sync per tool call. */
export function watchConfig(harness: string, cwd: string): { cfg: Config; bankId: string } | null {
  const base = loadConfig({ harness });
  const derived = deriveBankIdOrSkip(base, cwd, harness);
  if (derived === null) return null;
  const resolved = applyBankConfig(base, derived, cwd);
  return resolved.cfg.disabled || !watches(resolved.cfg, harness) ? null : resolved;
}

/** The session's sync, or null when the server has no sessions (the plugin then writes back itself). */
export async function syncHarnessSession(
  harness: string,
  ev: WatchEvent,
  resolved: { cfg: Config; bankId: string },
  turnEnd = false
): Promise<SyncResult | null> {
  const eventsOf = SESSION_HARNESSES[harness]?.eventsOf;
  const sessionId = ev.session_id;
  if (!eventsOf || !sessionId) return null;
  const cwd = ev.cwd ?? process.cwd();
  const { cfg, bankId } = resolved;
  const r = await syncSession({
    sessionId,
    transcriptPath: ev.transcript_path,
    cwd,
    dir: sessionStateDir(sessionId),
    client: new HindsightClient({ apiUrl: cfg.apiUrl, apiToken: cfg.apiToken, bank: bankId }),
    eventsOf,
    options: () =>
      sessionOptions({
        cfg,
        harness,
        sessionId,
        bankId,
        cwd,
        sessionRoot: sessionRootDir(harness, sessionId, cwd),
      }),
    ...(ev.prompt ? { prompt: { text: ev.prompt, ts: new Date().toISOString() } } : {}),
    turnEnd,
    ...(ev.reply ? { reply: ev.reply } : {}),
  });
  return r.unsupported ? null : r;
}

/** The background half: send what the session did, and leave its notes for the next tool call. */
async function backgroundSync(harness: string, ev: WatchEvent): Promise<void> {
  if (!ev.session_id) return;
  const resolved = watchConfig(harness, ev.cwd ?? process.cwd());
  if (!resolved) return;
  await ensureDaemon(resolved.cfg, harness, { waitMs: DAEMON_WAIT_RETAIN_MS });
  const synced = await syncHarnessSession(harness, ev, resolved);
  if (!synced) return;
  for (const note of synced.notes) stashPendingContext(notesKey(harness), ev.session_id, note);
  if (synced.notes.length) diag(harness, "watch_notes", { count: synced.notes.length });
}

async function main(harness: string): Promise<string | null> {
  // Grok Build runs Claude Code's hooks too (its compatibility layer): not this session's agent.
  if (process.env.HINDSIGHT_DISABLE_HOOKS || process.env.GROK_HOOK_EVENT) return null;
  if (process.argv[2] === "--work") {
    await backgroundSync(harness, JSON.parse(process.env[EVENT_ENV] ?? "{}") as WatchEvent);
    return null;
  }
  const ev = readHookEvent(harness) as WatchEvent | null;
  if (!ev?.session_id || !watchConfig(harness, ev.cwd ?? process.cwd())) return null;
  const note = takePendingContext(notesKey(harness), ev.session_id);
  if (note) diag(harness, "watch_delivered", { chars: note.length });
  // A server without sessions answered once: no sync per tool call for the rest of the session.
  if (!sessionsUnsupported(sessionStateDir(ev.session_id)))
    startSync(harness, {
      session_id: ev.session_id,
      transcript_path: ev.transcript_path,
      cwd: ev.cwd,
    });
  return note ?? null;
}

export function runWatchHook(harness: string): void {
  main(harness).then(
    (note) => {
      if (note)
        process.stdout.write(
          JSON.stringify({
            hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: note },
          })
        );
      process.exit(0);
    },
    (e) => {
      // A watcher failure must never reach the agent: stay silent, leave a trail.
      diag(harness, "watch_error", { error: describeError(e) });
      process.exit(0);
    }
  );
}
