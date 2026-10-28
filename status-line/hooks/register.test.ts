import { expect, test } from "claude-code/testing";

test("the end of a Claude turn saves context and plan usage for the panel", async ($, on) => {
  const written: string[] = [];
  on("env.get", () => ({ value: "C:/home" }) as never);
  on("session.id", () => ({ value: "abc" }) as never);
  on("clock.now", () => ({ value: 1000 }) as never);
  on(
    "session.usage",
    () =>
      ({
        value: {
          context: {
            breakdown: {
              totalTokens: 10,
              rawMaxTokens: 100,
              autoCompactThreshold: 60,
              categories: [{ name: "Messages", tokens: 10, kind: "used" }],
            },
          },
          rateLimits: [{ kind: "five_hour", percentUsed: 4, resetsAt: null }],
        },
      }) as never,
  );
  on("fs.write", (_$, e) => {
    written.push((e as { path: string }).path.replace(/\\/g, "/"));
    return { value: undefined } as never;
  });
  on("classic.Stop", () => ({}) as never);
  await $.classic.Stop({} as never);
  await new Promise((r) => setTimeout(r, 50));
  expect(written).toEqual([
    "C:/home/.claude/status-context/abc.json",
    "C:/home/.claude/status-usage/abc.json",
  ]);
});
