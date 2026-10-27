import { atom, read, update } from "claude-code";
import type { EngineInterface, Register } from "claude-code";

// A status line refeita como mod, para ter clique, na linha abaixo do prompt
// (PromptHint). Substitui a status line de comando: contexto, uso e custo vêm
// do próprio Claude Code ($.session.usage); cache, modelo e prompts do
// transcript; o effort real (o effort.level que a status line recebia) chega
// nos hooks de ferramenta. Clique no contexto abre o painel do que ocupa o
// contexto (com Compactar); /linha abre as ações num painel de teclado
// (Tab/setas, atalhos de uma tecla, Esc fecha). Clique no cache compacta, no effort
// ou no modelo abre um menu logo acima; cada
// tracinho é um prompt: passar o mouse mostra o texto, clicar rola até ele.

const MAX_PROMPTS = 15;

// O effort sobrevive ao reload do mod (as variáveis do módulo recomeçam).
const effortAtom = atom(
  { plugin: "linha-status", key: "effort" } as const,
  null,
);

// Do transcript: os prompts digitados (o uuid é o requestId da linha deles na
// conversa, então o clique rola até lá), a hora da última resposta da API com
// o TTL do cache que ela gravou e o modelo que respondeu.
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
  // Versão mais nova vista de cada família (ver learnModel).
  latest: Record<string, string>;
  ctx?: Ctx | "erro";
  now: number;
};

// O que ocupa o contexto, como o /context quebra (estimativa do Claude Code).
type Ctx = {
  total: number;
  max: number;
  rows: Array<[string, number]>;
  free: number;
  files: Array<[string, number]>;
  mcp: Array<[string, number]>;
};

const PANE_CTX = "linha-contexto";
const PANE_MENU = "linha-menu";

export function kTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1).replace(".", ",")}k` : String(n);
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

// O contexto por categoria (o mesmo do /context) num arquivo por sessão, ao fim de
// cada vez do Claude: o programa agentes (fora do Claude Code) desenha a barra com ele.
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
    title: "Linha",
    focus: true,
    closeOnEscape: true,
  });
}

async function openContext($: EngineInterface, st: State) {
  st.ctx = undefined;
  await $.ui.open({
    id: PANE_CTX,
    title: "Contexto",
    focus: true,
    closeOnEscape: true,
  });
  $.ui.invalidate("ui.render");
  try {
    const u = await $.session.usage({ breakdown: "summary" });
    st.ctx = u.context.breakdown ? toCtx(u.context.breakdown) : "erro";
  } catch {
    st.ctx = "erro";
  }
  $.ui.invalidate("ui.render");
}

// O menu troca de modelo pelo apelido da família, que o Claude Code sempre
// resolve para a versão mais nova. O nome mostrado é a versão mais nova já
// vista (sessão e transcript), guardada no $.store entre sessões; LATEST_SEED
// só vale até a primeira vez que uma versão nova aparece.
const FAMILIES = ["opus", "sonnet", "haiku", "fable"] as const;
const LATEST_SEED: Record<string, string> = {
  opus: "claude-opus-5-5",
  sonnet: "claude-sonnet-5-5",
  haiku: "claude-haiku-5-5",
  fable: "claude-fable-5-1",
};

// [família, maior, menor] de um id ("claude-opus-5-5[1m]" -> opus 5 5).
export function versionOf(id: string): [string, number, number] | undefined {
  const m = /(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?!\d)/i.exec(id);
  return m ? [m[1]!.toLowerCase(), Number(m[2]), Number(m[3] ?? 0)] : undefined;
}

// Guarda o id se for de versão mais nova que a conhecida da família.
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

// [apelido, nome] de cada família, com a versão mais nova conhecida.
export function modelOptions(
  latest: Record<string, string>,
): Array<[string, string]> {
  return FAMILIES.map((f) => [f, prettyModel(latest[f] ?? LATEST_SEED[f]!)]);
}

// A família do modelo atual, pelo id ou pelo apelido ("opus").
export function currentModelId(model: string | undefined): string | undefined {
  return /(opus|sonnet|haiku|fable)/i.exec(model ?? "")?.[1]?.toLowerCase();
}

// "claude-opus-5-5[1m]" -> "Opus 5.5", como o display_name da status line.
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
    // O consumo do plano é da conta, não da sessão: fica num arquivo para o
    // programa agentes (fora do Claude Code) mostrar também.
    // Cada sessão grava no seu arquivo: uma sessão parada tem o número da
    // última resposta dela (velho), e o programa escolhe a leitura mais nova.
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

// Hora local (o offset vem do Python: o ambiente do mod não sabe o fuso).
function clock(ms: number, tz: number, now: number): string {
  const d = new Date(ms + tz * 1000);
  const hm = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
  const today = new Date(now + tz * 1000).toISOString().slice(0, 10);
  if (d.toISOString().slice(0, 10) === today) return `às ${hm}`;
  const dias = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"];
  return `${dias[d.getUTCDay()]} ${String(d.getUTCDate()).padStart(2, "0")}/${String(d.getUTCMonth() + 1).padStart(2, "0")} às ${hm}`;
}

function span(ms: number): string {
  const m = Math.max(0, Math.round(ms / 60_000));
  if (m < 60) return `${m}min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h${String(m % 60).padStart(2, "0")}`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

// Clique no effort ou no modelo: abre (ou fecha) um menu logo acima dele.
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
    $.ui.toast("Não deu para trocar o effort agora.");
  }
}

// `family` é o apelido ("opus"): o Claude Code escolhe a versão mais nova.
async function pickModel($: EngineInterface, st: State, family: string) {
  st.menu = undefined;
  $.ui.invalidate("ui.render");
  // Quem está no contexto de 1M continua nele (Opus e Sonnet).
  const big = /\[1m\]/i.test(st.model ?? "") && /opus|sonnet/.test(family);
  const target = big ? `${family}[1m]` : family;
  try {
    await $.command.run({ command: "model", args: target });
    st.model = target;
    $.ui.invalidate("ui.render");
  } catch {
    $.ui.toast("Não deu para trocar o modelo agora.");
  }
}

// Clique no 🗂 artifacts: roda o /artifacts, como se você tivesse digitado
// (se a resposta estiver no meio, ele abre quando ela terminar).
async function openArtifacts($: EngineInterface) {
  try {
    await $.command.run({ command: "artifacts" });
  } catch {
    $.ui.toast("Não deu para abrir o /artifacts agora.");
  }
}

async function compact($: EngineInterface) {
  $.ui.toast("Compactando a conversa…");
  try {
    await $.session.compact();
  } catch {
    $.ui.toast("Não deu para compactar agora.");
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
      name: "linha",
      description:
        "Ações da linha de status pelo teclado (contexto, compactar, effort, modelo…)",
    });
    await $.command.register({
      name: "contexto",
      description: "O que está ocupando o contexto desta conversa",
    });
    return result;
  });

  on("command.run", { command: "linha" }, async ($) => {
    await openMenu($);
    return { text: "painel aberto (Esc fecha)" };
  });

  on("command.run", { command: "contexto" }, async ($) => {
    await openContext($, st);
    return { text: "painel aberto (Esc fecha)" };
  });

  // O painel de contexto: cada categoria com tokens e % da janela, maiores
  // primeiro; depois os arquivos de memória e os MCPs que mais pesam.
  on("ui.render", { component: "Pane", requestId: PANE_CTX }, ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e);
    const c = st.ctx;
    const actions = (
      <Box flexDirection="row" marginTop={1}>
        <Button
          key="ctx-compact"
          plain
          hotkey="k"
          label="Compactar"
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
          label="Atualizar"
          onPress={() => openContext($, st)}
        />
      </Box>
    );
    if (c === undefined)
      return (
        <Box flexDirection="column">
          <Text dimColor>Medindo o contexto…</Text>
        </Box>
      );
    if (c === "erro")
      return (
        <Box flexDirection="column">
          <Text>Não deu para medir agora. Tente o /context.</Text>
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
        >{`${kTokens(c.total)} de ${kTokens(c.max)} (${pct(c.total)})`}</Text>
        {c.rows.map(([n, t]) => row(n, t, `r${n}`))}
        {row("Livre", c.free, "free")}
        {c.files.length > 0 && (
          <Box marginTop={1}>
            <Text bold>Arquivos de memória que mais pesam</Text>
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
        {c.mcp.length > 0 && <Text bold>MCPs carregados</Text>}
        {c.mcp.map(([s, t]) => row(s, t, `m${s}`))}
        {actions}
      </Box>
    );
  });

  // O menu de teclado: Tab/setas andam, Enter ou a tecla entre [ ] aperta.
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
          label="Ver o que ocupa o contexto"
          onPress={done(() => openContext($, st))}
        />
        <Button
          key="k-compact"
          plain
          hotkey="k"
          label="Compactar a conversa"
          onPress={done(() => compact($))}
        />
        <Button
          key="k-arts"
          plain
          hotkey="a"
          label="Abrir artifacts"
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
          <Text bold>Modelo</Text>
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

  // O effort de verdade, depois de qualquer rebaixamento pelo modelo: chega em
  // cada hook de ferramenta e no fim da volta.
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
    // Um card que aparece por cima ao passar o mouse.
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
    // O menu do effort ou do modelo, aberto por cima, logo acima do botão.
    const menu = (kind: "effort" | "model") => {
      if (st.menu !== kind)
        return card(
          `clique: escolher o ${kind === "effort" ? "effort" : "modelo"}`,
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
          ? "🧊 cache expirou"
          : `🧊 cache ${left < 60_000 ? "<1" : Math.floor(left / 60_000)}m${left < 300_000 ? " ⚠" : ""}`;
      push(
        <Box key="cache">
          {card(
            `${left <= 0 ? "expirou" : "expira"} ${clock(expires, tz, st.now)} · clique compacta`,
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
                  ? `${l.label} volta a 0% ${clock(l.resetsAt, tz, st.now)} (daqui ${span(l.resetsAt - st.now)})`
                  : `${l.label}: sem hora de reset`,
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

    // Botão escondido: só existe para o ctrl+l abrir o /linha. O atalho é o do
    // app:cycleDiffBase (keybindings.json): o motor só deixa um botão de mod
    // pegar atalho de ação dele, e essa só existe com o painel de diff aberto.
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

    // Só a pasta: 🗂 artifacts abre o painel do /artifacts, com todos da conta.
    // Os desta sessão não viram links na linha (pedido do usuário: "só a pasta").
    push(
      <Box key="arts" flexDirection="row">
        <Box key="arts-all">
          {card("clique: abre todos os artifacts (o mesmo que /artifacts)")}
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
    if (!items.length) push(<Text dimColor>linha-status carregando…</Text>);

    return (
      <Box flexDirection="row" flexWrap="wrap">
        {items}
      </Box>
    );
  });
};
