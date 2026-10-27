import { expect, test } from "claude-code/testing";
import {
  currentModelId,
  kTokens,
  learnModel,
  modelOptions,
  toCtx,
} from "./register.tsx";

const HINT = { plugin: "linha-status", component: "PromptHint", props: {} };

test("clicar no effort abre o menu com os 5 níveis e escolher fecha", async ($, on) => {
  on("command.run", () => ({ text: "" }) as never);
  on("classic.Stop", () => ({}) as never);
  await $.classic.Stop({ effort: { level: "high" } } as never);
  for (const surface of ["terminal", "desktop"] as const) {
    const ui = await $.ui.mount({ ...HINT, surface } as never);
    expect(await ui.find({ key: "mmedium" } as never)).toBeUndefined();
    await ui.press({ key: "effort-open" } as never);
    for (const level of ["low", "medium", "high", "xhigh", "max"])
      expect(await ui.find({ key: `m${level}` } as never)).toBeDefined();
    await ui.press({ key: "mmedium" } as never);
    expect(await ui.find({ key: "mmedium" } as never)).toBeUndefined();
    await ui.unmount();
  }
});

test("clicar em 🗂 artifacts roda o /artifacts", async ($, on) => {
  const ran: string[] = [];
  on("command.run", (_$, e) => {
    ran.push((e as { command: string }).command);
    return { text: "" } as never;
  });
  const ui = await $.ui.mount({ ...HINT, surface: "terminal" } as never);
  await ui.press({ key: "arts-all-btn" } as never);
  expect(ran).toEqual(["artifacts"]);
  await ui.unmount();
});

test("toCtx: maiores primeiro, livre separado, MCP somado por servidor", () => {
  const c = toCtx({
    categories: [
      { name: "System prompt", tokens: 3000, kind: "used" },
      { name: "Messages", tokens: 50000, kind: "used" },
      { name: "Free space", tokens: 100000, kind: "free" },
      { name: "Autocompact buffer", tokens: 40000, kind: "buffer" },
    ],
    totalTokens: 53000,
    rawMaxTokens: 200000,
    memoryFiles: [{ path: "C:\\a\\CLAUDE.md", tokens: 900 }],
    mcpTools: [
      { serverName: "github", tokens: 400, isLoaded: true },
      { serverName: "github", tokens: 600, isLoaded: true },
      { serverName: "gmail", tokens: 999, isLoaded: false },
    ],
  });
  expect(c.rows.map((r) => r[0])).toEqual(["Messages", "System prompt"]);
  expect(c.free).toBe(100000);
  expect(c.mcp).toEqual([["github", 1000]]);
  expect(kTokens(53000)).toBe("53,0k");
});

test("/contexto abre o painel e mostra as categorias", async ($, on) => {
  on("session.usage", () => ({}) as never);
  const ui = await $.ui.mount({
    plugin: "linha-status",
    surface: "terminal",
    component: "Pane",
    requestId: "linha-contexto",
    props: {},
  } as never);
  expect(await ui.find({ text: /Medindo|Não deu|de / } as never)).toBeDefined();
  await ui.unmount();
});

test("/linha: menu de teclado com atalhos", async ($) => {
  const ui = await $.ui.mount({
    plugin: "linha-status",
    surface: "terminal",
    component: "Pane",
    requestId: "linha-menu",
    props: {},
  } as never);
  for (const key of ["k-ctx", "k-compact", "k-arts", "k-elow", "k-mopus"])
    expect(await ui.find({ key } as never)).toBeDefined();
  await ui.unmount();
});
test("⌨ na linha: botão com o atalho do app:cycleDiffBase", async ($) => {
  const ui = await $.ui.mount({ ...HINT, surface: "terminal" } as never);
  const btn = (await ui.find({ key: "kbd-btn" } as never)) as {
    props: { action?: string };
  };
  expect(btn.props.action).toBe("app:cycleDiffBase");
  await ui.unmount();
});
test("modelo atual: a família, pelo id ou pelo apelido", () => {
  expect(currentModelId("claude-opus-5-5[1m]")).toBe("opus");
  expect(currentModelId("opus")).toBe("opus");
  expect(currentModelId("Sonnet 5.5")).toBe("sonnet");
  expect(currentModelId(undefined)).toBe(undefined);
});

test("aprende a versão mais nova de cada família e ignora a mais velha", () => {
  const seed = { opus: "claude-opus-5-5", haiku: "claude-haiku-5-5" };
  const newer = learnModel(seed, "claude-opus-6-1[1m]");
  expect(newer.opus).toBe("claude-opus-6-1");
  expect(learnModel(newer, "claude-opus-5-5")).toBe(newer);
  expect(learnModel(seed, "opus")).toBe(seed);
  expect(learnModel(seed, null)).toBe(seed);
  expect(modelOptions(newer)).toEqual([
    ["opus", "Opus 6.1"],
    ["sonnet", "Sonnet 5.5"],
    ["haiku", "Haiku 5.5"],
    ["fable", "Fable 5.1"],
  ]);
});
