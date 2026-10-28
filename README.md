# claude agents mod

A terminal panel to follow and talk to Claude Code background sessions (`claude --bg`),
in place of `claude agents`. It lists each session with its model, context, cache and mode,
with the live conversation of the selected one beside the list. You can reply, queue messages,
answer questions and permissions, open files and links, and copy commands without entering
the session.

## Requirements

- Windows 10/11 with **Windows Terminal**.
- **Python 3.11** with the `py` launcher (`py -3.11 --version` must work).
- **Claude Code** installed (`claude --version`) and logged in.
- Git, to download and update.

## Install

```powershell
git clone https://github.com/Cebakz/claude-agents-mod.git
cd claude-agents-mod
powershell -ExecutionPolicy Bypass -File install.ps1 -StatusLine
```

Open a new PowerShell and run `claude agents mod`. The first time, it installs `pywinpty` and
`pyte` by itself. In cmd, the command is `claude-agents-mod`.

`-StatusLine` also installs the small `status-line` mod. It shows nothing inside the sessions:
at the end of each Claude turn it saves the context breakdown and the plan usage, so the panel
can show the context bar by category and the 5h / 7d usage. Without it the panel works the same,
just without those two. The installer copies the mod to `~/.claude/mods/status-line` and enables
it in `~/.claude/settings.json` (saving a backup of the file first). Sessions that are already
open pick up the mod with `/reload-plugins`.

## Update

```powershell
cd claude-agents-mod
git pull
powershell -ExecutionPolicy Bypass -File install.ps1 -StatusLine
```

With the panel open, F5 reloads the program.

## Use

- **Pick and follow:** arrows or a click pick a session in the list; its conversation shows
  beside it. Enter or a double click opens the session.
- **Reply:** type and press enter. Shift+enter adds a line; ↑ with an empty box (space opens
  it) brings back your previous prompts. Tab queues the message; alt+↑ edits the queue.
- **Questions and permissions:** they show below the conversation. The number picks the
  option; your own text plus enter answers in "Type something"; "1 3" selects several.
- **Files:** files mentioned in the conversation become buttons (open, ⧉ copy file to paste
  into Teams / WhatsApp, ⌕ in folder). Dragging the mouse over the conversation selects and
  copies.
- **⊞ Features** (at the top): turns each panel feature on and off. **? Shortcuts** lists the
  keys.

## Things to know

- Windows only (it reads keyboard and mouse through the Windows console).
- It reads Claude Code internal files (`~/.claude/jobs/*/state.json` and the `.jsonl`
  conversations), which are not documented: a Claude Code update may change their format and
  break something.
- To write into a session it opens a hidden `claude attach`. With a question or permission
  open it never presses Enter by itself, so it never approves anything for you.
- The SUMMARY at the top of each conversation is written by Haiku and uses tokens; turn it off
  in ⊞ Features.
- Your data (groups, queue, panel settings) lives in `~/.claude/agents-mod/`.
- `AGENTS_KEYLOG=1` turns on a key log in `~/.claude/agents-mod/keys.log`, to track down
  keyboard bugs. Off by default: it records everything you type.
- Independent project, not affiliated with Anthropic.

## License

MIT, see [LICENSE](LICENSE).
