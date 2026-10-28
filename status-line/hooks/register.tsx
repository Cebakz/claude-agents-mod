import { atom, read, update } from "claude-code";
import type { EngineInterface, Register } from "claude-code";

// The status line rebuilt as a mod, so it is clickable, on the line below the
// prompt (PromptHint). It replaces the command status line: context, usage and
// cost come from Claude Code itself ($.session.usage); cache, model and prompts
// from the transcript; the real effort (the effort.level the status line used
// to receive) arrives in the tool hooks. Clicking the context opens the panel
// showing what fills the context (with Compact); /status-actions opens the
// actions in a keyboard panel (Tab/arrows, one-key shortcuts, Esc closes).
// Clicking the cache compacts; clicking the effort or the model opens a menu
// just above it; each dash is a prompt: hover shows the text, click scrolls
// to it.

const MAX_PROMPTS = 15;

// The effort survives a mod reload (module variables start over).
const effortAtom = atom(
  { plugin: "status-line", key: "effort" } as const,
  null,
);

// From the transcript: the typed prompts (the uuid is the requestId of their
// line in the conversation, so a click scrolls there), the time of the last API
// response with the cache TTL it wrote, and the model that answered.
const TRANSCRIPT_PY = String.raw`
import sys, json
sys.stdout.reconfigure(encoding="utf-8")
out = []
last_ts, ttl, model = None, None, None
skip = ("<", "[Request interrupted", "Caveat:")
with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
    for line in f:
        if '"usage"' in line and '"type":"assistant"' in line:
            try:
                d = json.loads(line)
                cc = (d["message"].get("usage") or {}).get("cache_creation") or {}
                last_ts = d.get("timestamp") or last_ts
                model = d["message"].get("model") or model
                if cc.get("ephemeral_1h_input_tokens"):
                    ttl = 3600
                elif cc.get("ephemeral_5m_input_tokens"):
                    ttl = 300
            except Exception:
                pass
            continue
        if '"type":"user"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
            continue
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                continue
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        if not isinstance(c, str):
            continue
        t = " ".join(c.split())
        if not t or t.startswith(skip):
            continue
        out.append([d.get("uuid"), t[:300]])
import time
print(json.dumps({"tz": time.localtime().tm_gmtoff, "total": len(out), "last": out[-int(sys.argv[2]):], "lastTs": last_ts, "ttl": ttl or 3600, "model": model}, ensure_ascii=False))
`;

type Transcript = {
  total: number;
  last: Array<[string, string]>;
  lastTs?: string | null;
  ttl?: number;
  model?: string | null;
  tz?: number;
};

type Snapshot = {
  contextPct?: number;
  limits: Array<{ label: string; pct: number; resetsAt?: number }>;
  branch?: string;
};

type State = {
  snap: Snapshot;
  tr: Transcript;
  effort?: string;
  model?: string;
  menu?: "effort" | "model";
  // Newest version seen for each family (see learnModel).
  latest: Record<string, string>;
  ctx?: Ctx | "error";
  now: number;
};

// What fills the context, as /context breaks it down (Claude Code's estimate).
type Ctx = {
  total: number;
  max: number;
  rows: Array<[string, number]>;
  free: number;
  files: Array<[string, number]>;
  mcp: Array<[string, number]>;
};

const PANE_CTX = "status-context";
const PANE_MENU = "status-menu";

export function kTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function toCtx(b: {
  categories: Array<{ name: string; tokens: number; kind: string }>;
  totalTokens: number;
  rawMaxTokens: number;
  memoryFiles: Array<{ path: string; tokens: number }>;
  mcpTools: Array<{ serverName: string; tokens: number; isLoaded: boolean }>;
}): Ctx {
  const mcp = new Map<string, number>();
  for (const t of b.mcpTools)
    if (t.isLoaded)
      mcp.set(t.serverName, (mcp.get(t.serverName) ?? 0) + t.tokens);
  const desc = (a: [string, number], z: [string, number]) => z[1] - a[1];
  return {
    total: b.totalTokens,
    max: b.rawMaxTokens,
    rows: b.categories
      .filter((c) => c.kind === "used" && c.tokens > 0)
      .map((c): [string, number] => [c.name, c.tokens])
      .sort(desc),
    free: b.categories
      .filter((c) => c.kind === "free")
      .reduce((s, c) => s + c.tokens, 0),
    files: b.memoryFiles
      .map((f): [string, number] => [f.path, f.tokens])
      .sort(desc)
      .slice(0, 5),
    mcp: [...mcp.entries()].sort(desc).slice(0, 5),
  };
}

// The context by category (same as /context) in one file per session, at the end
// of each Claude turn: the agents program (outside Claude Code) draws the bar from it.
async function saveCtx($: EngineInterface) {
  try {
    const home = (await $.env.get("USERPROFILE")) ?? (await $.env.get("HOME"));
    const sid = await $.session.id();
    if (!home || !sid) return;
    const u = await $.session.usage({ breakdown: "summary" });
    const b = u.context.breakdown;
    if (!b) return;
    await $.fs.write(
      `${home}/.claude/linha-contexto/${sid}.json`,
      JSON.stringify({
        ts: await $.clock.now(),
        total: b.totalTokens,
        max: b.rawMaxTokens,
        compactAt: b.autoCompactThreshold ?? null,
        rows: b.categories.map((c) => ({
          name: c.name,
          tokens: c.tokens,
          kind: c.kind,
        })),
      }),
    );
  } catch {}
}

async function openMenu($: EngineInterface) {
  await $.ui.open({
    id: PANE_MENU,
    title: "Status line",
    focus: true,
    closeOnEscape: true,
  });
}

async function openContext($: EngineInterface, st: State) {
  st.ctx = undefined;
  await $.ui.open({
    id: PANE_CTX,
    title: "Context",
    focus: true,
    closeOnEscape: true,
  });
  $.ui.invalidate("ui.render");
  try {
    const u = await $.session.usage({ breakdown: "summary" });
    st.ctx = u.context.breakdown ? toCtx(u.context.breakdown) : "error";
  } catch {
    st.ctx = "error";
  }
  $.ui.invalidate("ui.render");
}

// The menu switches model by the family alias, which Claude Code always
// resolves to the newest version. The name shown is the newest version seen so
// far (session and transcript), kept in $.store across sessions; LATEST_SEED
// only applies until a newer version first shows up.
const FAMILIES = ["opus", "sonnet", "haiku", "fable"] as const;
const LATEST_SEED: Record<string, string> = {
  opus: "claude-opus-5-5",
  sonnet: "claude-sonnet-5-5",
  haiku: "claude-haiku-5-5",
  fable: "claude-fable-5-1",
};

// [family, major, minor] of an id ("claude-opus-5-5[1m]" -> opus 5 5).
export function versionOf(id: string): [string, number, number] | undefined {
  const m = /(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?!\d)/i.exec(id);
  return m ? [m[1]!.toLowerCase(), Number(m[2]), Number(m[3] ?? 0)] : undefined;
}

// Keeps the id if it is a newer version than the one known for the family.
export function learnModel(
  latest: Record<string, string>,
  id: string | null | undefined,
): Record<string, string> {
  const v = id ? versionOf(id) : undefined;
  if (!v) return latest;
  const known = latest[v[0]] ? versionOf(latest[v[0]]!) : undefined;
  if (known && (known[1] > v[1] || (known[1] === v[1] && known[2] >= v[2])))
    return latest;
  return { ...latest, [v[0]]: id!.replace(/\[.*\]$/, "") };
}

// [alias, name] of each family, with the newest known version.
export function modelOptions(
  latest: Record<string, string>,
): Array<[string, string]> {
  return FAMILIES.map((f) => [f, prettyModel(latest[f] ?? LATEST_SEED[f]!)]);
}

// The family of the current model, from the id or the alias ("opus").
export function currentModelId(model: string | undefined): string | undefined {
  return /(opus|sonnet|haiku|fable)/i.exec(model ?? "")?.[1]?.toLowerCase();
}

// "claude-opus-5-5[1m]" -> "Opus 5.5", like the status line display_name.
export function prettyModel(id: string): string {
  const m = /(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?!\d)/i.exec(id);
  if (!m)
    return id
      .replace(/^Claude /, "")
      .split("(")[0]
      .trim();
  const name = m[1][0].toUpperCase() + m[1].slice(1).toLowerCase();
  return m[3] ? `${name} ${m[2]}.${m[3]}` : `${name} ${m[2]}`;
}

async function run($: EngineInterface, argv: string[], cwd?: string) {
  try {
    const r = await $.process.run(argv, { cwd, timeoutMs: 5000 });
    return r.exitCode === 0 ? r.stdout : "";
  } catch {
    return "";
  }
}

async function readState($: EngineInterface, st: State) {
  const now = await $.clock.now();
  const home = (await $.env.get("USERPROFILE")) ?? (await $.env.get("HOME"));
  const sid = await $.session.id();
  const cwd = await $.session.cwd();
  const snap: Snapshot = { limits: [] };

  try {
    const usage = await $.session.usage();
    snap.contextPct = usage.context.percent;
    const label: Record<string, string> = {
      five_hour: "5h",
      seven_day: "7d",
      spend_limit: "Spend",
    };
    for (const l of usage.rateLimits) {
      const reset = l.resetsAt ? Date.parse(l.resetsAt) : NaN;
      const pct = reset < now ? 0 : l.percentUsed;
      snap.limits.push({
        label: label[l.kind] ?? l.kind,
        pct: Math.round(pct),
        resetsAt: reset >= now ? reset : undefined,
      });
    }
    // Plan usage belongs to the account, not the session: it goes in a file so
    // the agents program (outside Claude Code) can show it too.
    // Each session writes its own file: an idle session has the number from its
    // last response (stale), and the program picks the newest reading.
    if (snap.limits.length && home && sid)
      void $.fs
        .write(
          `${home}/.claude/linha-uso/${sid}.json`,
          JSON.stringify({ ts: now, limits: snap.limits }),
        )
        .catch(() => {});
  } catch {}

  snap.branch =
    (await run($, ["git", "symbolic-ref", "--short", "HEAD"], cwd)).trim() ||
    undefined;

  const transcript = `${home}/.claude/projects/${cwd.replace(/[^A-Za-z0-9]/g, "-")}/${sid}.jsonl`;
  try {
    st.tr = JSON.parse(
      await run($, [
        "py",
        "-3.11",
        "-c",
        TRANSCRIPT_PY,
        transcript,
        String(MAX_PROMPTS),
      ]),
    ) as Transcript;
  } catch {}

  try {
    st.model = (await $.session.model()) || st.model;
  } catch {}

  const latest = learnModel(learnModel(st.latest, st.model), st.tr.model);
  if (latest !== st.latest) {
    st.latest = latest;
    try {
      await $.store.set("latest-models", latest);
    } catch {}
  }

  st.snap = snap;
  st.now = now;
  $.ui.invalidate("ui.render");
}

// Local time (the offset comes from Python: the mod environment does not know the time zone).
function clock(ms: number, tz: number, now: number): string {
  const d = new Date(ms + tz * 1000);
  const hm = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
  const today = new Date(now + tz * 1000).toISOString().slice(0, 10);
  if (d.toISOString().slice(0, 10) === today) return `at ${hm}`;
  const dias = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return `${dias[d.getUTCDay()]} ${String(d.getUTCDate()).padStart(2, "0")}/${String(d.getUTCMonth() + 1).padStart(2, "0")} at ${hm}`;
}

function span(ms: number): string {
  const m = Math.max(0, Math.round(ms / 60_000));
  if (m < 60) return `${m}min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h${String(m % 60).padStart(2, "0")}`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

// Click on the effort or the model: opens (or closes) a menu just above it.
function toggleMenu($: EngineInterface, st: State, kind: "effort" | "model") {
  st.menu = st.menu === kind ? undefined : kind;
  $.ui.invalidate("ui.render");
}

async function pickEffort($: EngineInterface, st: State, level: string) {
  st.menu = undefined;
  $.ui.invalidate("ui.render");
  try {
    await $.command.run({ command: "effort", args: level });
    st.effort = level;
    await update($, effortAtom, () => level);
    $.ui.invalidate("ui.render");
  } catch {
    $.ui.toast("Could not change the effort right now.");
  }
}

// `family` is the alias ("opus"): Claude Code picks the newest version.
async function pickModel($: EngineInterface, st: State, family: string) {
  st.menu = undefined;
  $.ui.invalidate("ui.render");
  // Whoever is on the 1M context stays on it (Opus and Sonnet).
  const big = /\[1m\]/i.test(st.model ?? "") && /opus|sonnet/.test(family);
  const target = big ? `${family}[1m]` : family;
  try {
    await $.command.run({ command: "model", args: target });
    st.model = target;
    $.ui.invalidate("ui.render");
  } catch {
    $.ui.toast("Could not change the model right now.");
  }
}

// Click on 🗂 artifacts: runs /artifacts, as if you had typed it
// (if a response is in progress, it opens when the response finishes).
async function openArtifacts($: EngineInterface) {
  try {
    await $.command.run({ command: "artifacts" });
  } catch {
    $.ui.toast("Could not open /artifacts right now.");
  }
}

async function compact($: EngineInterface) {
  $.ui.toast("Compacting the conversation…");
  try {
    await $.session.compact();
  } catch {
    $.ui.toast("Could not compact right now.");
  }
}

export const register: Register = (on) => {
  const st: State = {
    snap: { limits: [] },
    tr: { total: 0, last: [] },
    latest: LATEST_SEED,
    now: 0,
  };

  on("session.start", async ($, e, next) => {
    const result = await next(e);
    st.effort = (await read($, effortAtom)) ?? st.effort;
    try {
      const saved = (await $.store.get("latest-models")) as
        | Record<string, string>
        | undefined;
      for (const id of Object.values(saved ?? {}))
        st.latest = learnModel(st.latest, id);
    } catch {}
    void readState($, st);
    $.clock.every(15_000, () => void readState($, st));
    await $.command.register({
      name: "status-actions",
      description:
        "Status line actions from the keyboard (context, compact, effort, model…)",
    });
    await $.command.register({
      name: "context-panel",
      description: "What is filling the context of this conversation",
    });
    return result;
  });

  on("command.run", { command: "status-actions" }, async ($) => {
    await openMenu($);
    return { text: "panel open (Esc closes)" };
  });

  on("command.run", { command: "context-panel" }, async ($) => {
    await openContext($, st);
    return { text: "panel open (Esc closes)" };
  });

  // The context panel: each category with tokens and % of the window, largest
  // first; then the heaviest memory files and MCPs.
  on("ui.render", { component: "Pane", requestId: PANE_CTX }, ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e);
    const c = st.ctx;
    const actions = (
      <Box flexDirection="row" marginTop={1}>
        <Button
          key="ctx-compact"
          plain
          hotkey="k"
          label="Compact"
          onPress={() => {
            void $.ui.close({ id: PANE_CTX });
            void compact($);
          }}
        />
        <Text> </Text>
        <Button
          key="ctx-refresh"
          plain
          hotkey="r"
          label="Refresh"
          onPress={() => openContext($, st)}
        />
      </Box>
    );
    if (c === undefined)
      return (
        <Box flexDirection="column">
          <Text dimColor>Measuring the context…</Text>
        </Box>
      );
    if (c === "error")
      return (
        <Box flexDirection="column">
          <Text>Could not measure right now. Try /context.</Text>
          {actions}
        </Box>
      );
    const pct = (n: number) => `${Math.round((n / c.max) * 100)}%`;
    const bar = (n: number) =>
      "█".repeat(Math.max(1, Math.round((n / c.max) * 20)));
    const row = (name: string, n: number, k: string) => (
      <Box key={k} flexDirection="row">
        <Box width={24}>
          <Text wrap="truncate">{name}</Text>
        </Box>
        <Box width={8}>
          <Text>{kTokens(n)}</Text>
        </Box>
        <Box width={5}>
          <Text dimColor>{pct(n)}</Text>
        </Box>
        <Text color="permission">{bar(n)}</Text>
      </Box>
    );
    return (
      <Box flexDirection="column">
        <Text
          bold
        >{`${kTokens(c.total)} of ${kTokens(c.max)} (${pct(c.total)})`}</Text>
        {c.rows.map(([n, t]) => row(n, t, `r${n}`))}
        {row("Free", c.free, "free")}
        {c.files.length > 0 && (
          <Box marginTop={1}>
            <Text bold>Heaviest memory files</Text>
          </Box>
        )}
        {c.files.map(([p, t]) => (
          <Box key={`f${p}`} flexDirection="row">
            <Box width={8}>
              <Text>{kTokens(t)}</Text>
            </Box>
            <Text wrap="truncate-start">{p}</Text>
          </Box>
        ))}
        {c.mcp.length > 0 && <Text bold>Loaded MCPs</Text>}
        {c.mcp.map(([s, t]) => row(s, t, `m${s}`))}
        {actions}
      </Box>
    );
  });

  // The keyboard menu: Tab/arrows move, Enter or the key in [ ] presses.
  on("ui.render", { component: "Pane", requestId: PANE_MENU }, ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e);
    const done = (fn: () => unknown) => () => {
      void $.ui.close({ id: PANE_MENU });
      void fn();
    };
    const currentModel = currentModelId(st.model);
    return (
      <Box flexDirection="column">
        <Button
          key="k-ctx"
          plain
          hotkey="c"
          autoFocus
          label="See what fills the context"
          onPress={done(() => openContext($, st))}
        />
        <Button
          key="k-compact"
          plain
          hotkey="k"
          label="Compact the conversation"
          onPress={done(() => compact($))}
        />
        <Button
          key="k-arts"
          plain
          hotkey="a"
          label="Open artifacts"
          onPress={done(() => openArtifacts($))}
        />
        <Box marginTop={1}>
          <Text bold>Effort</Text>
        </Box>
        {EFFORTS.map((l, i) => (
          <Button
            key={`k-e${l}`}
            plain
            hotkey={String(i + 1)}
            label={`${l === st.effort ? "●" : "○"} ${l}`}
            onPress={done(() => pickEffort($, st, l))}
          />
        ))}
        <Box marginTop={1}>
          <Text bold>Model</Text>
        </Box>
        {modelOptions(st.latest).map(([id, label], i) => (
          <Button
            key={`k-m${id}`}
            plain
            hotkey={String(i + 6)}
            label={`${id === currentModel ? "●" : "○"} ${label}`}
            onPress={done(() => pickModel($, st, id))}
          />
        ))}
      </Box>
    );
  });

  on("turn.start", async ($, e, next) => {
    void readState($, st);
    return next(e);
  });

  on("turn.complete", async ($, e, next) => {
    const result = await next(e);
    if (e.agentId === undefined) void readState($, st);
    return result;
  });

  // The real effort, after any downgrade by the model: it arrives in each
  // tool hook and at the end of the turn.
  on("classic.PostToolUse", async ($, e, next) => {
    if (e.effort?.level && e.effort.level !== st.effort) {
      st.effort = e.effort.level;
      await update($, effortAtom, () => st.effort ?? null);
      $.ui.invalidate("ui.render");
    }
    return next(e);
  });
  on("classic.Stop", async ($, e, next) => {
    void saveCtx($);
    if (e.effort?.level && e.effort.level !== st.effort) {
      st.effort = e.effort.level;
      await update($, effortAtom, () => st.effort ?? null);
      $.ui.invalidate("ui.render");
    }
    return next(e);
  });

  on("ui.render", { component: "PromptHint" }, ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e);
    const s = st.snap;
    const tz = st.tr.tz ?? 0;
    // A card that appears on top on hover.
    const card = (text: string) => (
      <Box
        position="absolute"
        top={-1}
        left={0}
        display="none"
        hover={{ display: "flex" }}
        backgroundColor="gray"
      >
        <Text color="white" wrap="truncate">{` ${text} `}</Text>
      </Box>
    );
    // The effort or model menu, opened on top, just above the button.
    const menu = (kind: "effort" | "model") => {
      if (st.menu !== kind)
        return card(
          `click: choose the ${kind === "effort" ? "effort" : "model"}`,
        );
      const options: Array<[string, string]> =
        kind === "effort"
          ? EFFORTS.map((l) => [l, l])
          : modelOptions(st.latest);
      const current = kind === "effort" ? st.effort : currentModelId(st.model);
      return (
        <Box
          position="absolute"
          top={-1}
          left={0}
          flexDirection="row"
          backgroundColor="gray"
        >
          {options.map(([value, label]) => (
            <Button
              key={`m${value}`}
              plain
              label={` ${value === current ? "●" : "○"} ${label} `}
              onPress={() =>
                kind === "effort"
                  ? pickEffort($, st, value)
                  : pickModel($, st, value)
              }
            />
          ))}
        </Box>
      );
    };
    const items: unknown[] = [];
    const push = (node: unknown) => {
      if (items.length) items.push(<Text dimColor> | </Text>);
      items.push(node);
    };

    if (s.contextPct !== undefined) {
      const dot = s.contextPct < 50 ? "🟢" : s.contextPct < 80 ? "🟡" : "🔴";
      push(
        <Button
          key="ctx"
          plain
          label={`${dot} ${Math.round(s.contextPct)}%`}
          onPress={() => openContext($, st)}
        />,
      );
    }
    if (st.tr.lastTs) {
      const expires = Date.parse(st.tr.lastTs) + (st.tr.ttl ?? 3600) * 1000;
      const left = expires - st.now;
      const label =
        left <= 0
          ? "🧊 cache expired"
          : `🧊 cache ${left < 60_000 ? "<1" : Math.floor(left / 60_000)}m${left < 300_000 ? " ⚠" : ""}`;
      push(
        <Box key="cache">
          {card(
            `${left <= 0 ? "expired" : "expires"} ${clock(expires, tz, st.now)} · click to compact`,
          )}
          <Button plain label={label} onPress={() => compact($)} />
        </Box>,
      );
    }
    if (s.branch) push(<Text key="branch">{`🌿 ${s.branch}`}</Text>);
    if (s.limits.length)
      push(
        <Box key="limits" flexDirection="row">
          {s.limits.map((l, i) => (
            <Box key={`l${l.label}`}>
              {card(
                l.resetsAt
                  ? `${l.label} resets to 0% ${clock(l.resetsAt, tz, st.now)} (in ${span(l.resetsAt - st.now)})`
                  : `${l.label}: no reset time`,
              )}
              <Text>{`${i ? " " : ""}${l.label}:${l.pct}%`}</Text>
            </Box>
          ))}
        </Box>,
      );
    if (st.effort)
      push(
        <Box key="effort">
          {menu("effort")}
          <Button
            plain
            label={`⚡ ${st.effort}`}
            key="effort-open"
            onPress={() => toggleMenu($, st, "effort")}
          />
        </Box>,
      );
    const model = st.model ?? st.tr.model;
    if (model)
      push(
        <Box key="model">
          {menu("model")}
          <Button
            plain
            label={prettyModel(model)}
            key="model-open"
            onPress={() => toggleMenu($, st, "model")}
          />
        </Box>,
      );

    // Hidden button: it only exists so ctrl+l opens /status-actions. The shortcut
    // is the one for app:cycleDiffBase (keybindings.json): the engine only lets a
    // mod button take a shortcut for its own action, and that one only exists
    // with the diff panel open.
    items.push(
      <Box key="kbd" display="none">
        <Button
          plain
          key="kbd-btn"
          label="⌨"
          action="app:cycleDiffBase"
          onPress={() => openMenu($)}
        />
      </Box>,
    );

    // Folder only: 🗂 artifacts opens the /artifacts panel, with all the account's.
    // This session's artifacts do not become links on the line (user request: "folder only").
    push(
      <Box key="arts" flexDirection="row">
        <Box key="arts-all">
          {card("click: opens all artifacts (same as /artifacts)")}
          <Button
            plain
            key="arts-all-btn"
            label="🗂  artifacts"
            onPress={() => openArtifacts($)}
          />
        </Box>
      </Box>,
    );

    const list = st.tr.last;
    const first = st.tr.total - list.length;
    if (list.length)
      push(
        <Box key="prompts" flexDirection="row">
          {list.map(([id, text], i) => (
            <Box key={`t${first + i}`}>
              <Box
                position="absolute"
                top={-1}
                left={-i}
                display="none"
                hover={{ display: "flex" }}
                backgroundColor="gray"
              >
                <Text color="white" wrap="truncate">
                  {` ${first + i + 1}. ${text.length > 90 ? `${text.slice(0, 87)}...` : text} `}
                </Text>
              </Box>
              <Button
                key={`p${first + i}`}
                plain
                label={i === list.length - 1 ? "┃" : "│"}
                onPress={() => $.ui.scroll({ to: { requestId: id } })}
              />
            </Box>
          ))}
        </Box>,
      );
    if (!items.length) push(<Text dimColor>status-line loading…</Text>);

    return (
      <Box flexDirection="row" flexWrap="wrap">
        {items}
      </Box>
    );
  });
};
