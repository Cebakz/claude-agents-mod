import { expect, test } from "claude-code/testing";
import {
  currentModelId,
  kTokens,
  learnModel,
  modelOptions,
  toCtx,
} from "./register.tsx";

const HINT = { plugin: "status-line", component: "PromptHint", props: {} };

test("clicking the effort opens the menu with the 5 levels and picking one closes it", async ($, on) => {
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

test("clicking 🗂 artifacts runs /artifacts", async ($, on) => {
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

test("toCtx: largest first, free separate, MCP summed per server", () => {
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
  expect(kTokens(53000)).toBe("53.0k");
});

test("/context-panel opens the pane and shows the categories", async ($, on) => {
  on("session.usage", () => ({}) as never);
  const ui = await $.ui.mount({
    plugin: "status-line",
    surface: "terminal",
    component: "Pane",
    requestId: "status-context",
    props: {},
  } as never);
  expect(await ui.find({ text: /Measuring|Could not| of / } as never)).toBeDefined();
  await ui.unmount();
});

test("/status-actions: keyboard menu with shortcuts", async ($) => {
  const ui = await $.ui.mount({
    plugin: "status-line",
    surface: "terminal",
    component: "Pane",
    requestId: "status-menu",
    props: {},
  } as never);
  for (const key of ["k-ctx", "k-compact", "k-arts", "k-elow", "k-mopus"])
    expect(await ui.find({ key } as never)).toBeDefined();
  await ui.unmount();
});
test("⌨ on the line: button with the app:cycleDiffBase shortcut", async ($) => {
  const ui = await $.ui.mount({ ...HINT, surface: "terminal" } as never);
  const btn = (await ui.find({ key: "kbd-btn" } as never)) as {
    props: { action?: string };
  };
  expect(btn.props.action).toBe("app:cycleDiffBase");
  await ui.unmount();
});
test("current model: the family, from the id or the alias", () => {
  expect(currentModelId("claude-opus-5-5[1m]")).toBe("opus");
  expect(currentModelId("opus")).toBe("opus");
  expect(currentModelId("Sonnet 5.5")).toBe("sonnet");
  expect(currentModelId(undefined)).toBe(undefined);
});

test("learns the newest version of each family and ignores the older one", () => {
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
