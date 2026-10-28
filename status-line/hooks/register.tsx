import type { EngineInterface, Register } from "claude-code";

// Shows nothing on screen. It feeds the agents panel (claude agents mod), which runs outside
// Claude Code and cannot ask the session anything:
// - at the end of each Claude turn, the context by category (the same as /context), which
//   becomes the colored bar below the conversation;
// - the plan usage (5h, 7d), shown at the top of the panel.

// The context by category, in one file per session.
async function saveCtx($: EngineInterface, home: string, sid: string) {
  try {
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

// Plan usage belongs to the account, not the session. Each session writes its own file: an idle
// session holds the number from its last reply (stale), and the panel picks the newest reading.
async function savePlan($: EngineInterface, home: string, sid: string) {
  try {
    const now = await $.clock.now();
    const usage = await $.session.usage();
    const label: Record<string, string> = {
      five_hour: "5h",
      seven_day: "7d",
      spend_limit: "Spend",
    };
    const limits = usage.rateLimits.map((l) => {
      const reset = l.resetsAt ? Date.parse(l.resetsAt) : NaN;
      return {
        label: label[l.kind] ?? l.kind,
        pct: Math.round(reset < now ? 0 : l.percentUsed),
        resetsAt: reset >= now ? reset : undefined,
      };
    });
    if (limits.length)
      await $.fs.write(
        `${home}/.claude/linha-uso/${sid}.json`,
        JSON.stringify({ ts: now, limits }),
      );
  } catch {}
}

async function save($: EngineInterface, ctx: boolean) {
  try {
    const home = (await $.env.get("USERPROFILE")) ?? (await $.env.get("HOME"));
    const sid = await $.session.id();
    if (!home || !sid) return;
    if (ctx) await saveCtx($, home, sid);
    await savePlan($, home, sid);
  } catch {}
}

export const register: Register = (on) => {
  on("session.start", async ($, e, next) => {
    const result = await next(e);
    void save($, false);
    return result;
  });
  on("turn.start", async ($, e, next) => {
    void save($, false);
    return next(e);
  });
  on("classic.Stop", async ($, e, next) => {
    void save($, true);
    return next(e);
  });
};
