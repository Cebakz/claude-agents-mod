"""Agents: a replica of `claude agents` with a model column for each session.

Same screen, same groups, same pins (reads and writes ~/.claude/jobs/pins.json,
Claude's own file) and the same shortcuts:

  enter     open the session (claude attach) · on a group title, collapse
  space     reply to the selected session
  ctrl+x    delete (press twice) · on a title, deletes the whole group
  ctrl+f    search            alt+↑/↓  jump between groups
  ctrl+j    new line          ctrl+p   pin / unpin (extra in this replica)
  ?         shortcuts         esc      quit
  type      describes a task; enter creates the new session

Inside the open session, Ctrl+Z comes back to this list.
"""

import ctypes
import glob
import json
import msvcrt
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime

# The first time, installs the two outside libraries it uses (the rest is standard).
try:
    import pyte  # noqa: F401
    import winpty  # noqa: F401
except ImportError:
    print("Installing pywinpty and pyte (first run only)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "--disable-pip-version-check", "pywinpty", "pyte"])
    import importlib
    import site

    site.addsitedir(site.getusersitepackages())  # the --user folder may have just been created
    importlib.invalidate_caches()

HOME = os.path.expanduser("~/.claude")
PINS = os.path.join(HOME, "jobs", "pins.json")
# Your data (groups, view, tasks, queue) lives outside the program folder, so it can
# live in a repository without dirtying it. The first time, brings over the ones next to the script.
DATA_DIR = os.path.join(HOME, "agents-mod")
os.makedirs(DATA_DIR, exist_ok=True)
for _name in ("groups.json", "config.json", "tasks.json", "queue.json"):
    _old = os.path.join(os.path.dirname(os.path.abspath(__file__)), _name)
    if os.path.exists(_old) and not os.path.exists(os.path.join(DATA_DIR, _name)):
        shutil.copy2(_old, os.path.join(DATA_DIR, _name))
GROUPS_FILE = os.path.join(DATA_DIR, "groups.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")   # queued messages (tab)
# Log of raw keys, to find keyboard bugs: only with AGENTS_KEYLOG=1 (records everything
# you type, including pasted passwords).
KEYS_LOG = os.path.join(DATA_DIR, "keys.log") if os.environ.get("AGENTS_KEYLOG") == "1" else None
# Projects view (Paperclip style): goals on the left, the goal's tasks on the right.
# A task's column comes from its agents; only "Done" is marked by you.
BOARD = [("todo", "To do"), ("working", "Working"), ("review", "Review"), ("done", "Done")]
COLUMN_DOTS = ["\x1b[38;2;140;140;140m●\x1b[0m", "\x1b[38;2;90;150;235m●\x1b[0m",
               "\x1b[38;2;232;181;66m●\x1b[0m", "\x1b[38;2;177;151;252m●\x1b[0m", "\x1b[38;2;78;186;101m●\x1b[0m",
               "\x1b[38;2;215;119;87m●\x1b[0m"]
SETTINGS = os.path.join(HOME, "settings.json")
REFRESH_S = 3
MODEL = re.compile(rb'"model":"(claude-[^"]+)"')
EFFORT = re.compile(rb'"effort":"([a-z]+)"')

ESC = "\x1b["
# Modes that Claude turns on in the terminal and sometimes leaves on when leaving the session:
# Windows Terminal keyboard, kitty keyboard, bracketed paste, focus and mouse.
TERMINAL_RESET = "".join(ESC + m for m in ("?9001l", "<u", "?2004l", "?1004l", "?1000l", "?1002l", "?1003l", "?1006l", ">4;0m"))
RESET = ESC + "0m"
BOLD = ESC + "1m"
DIM = ESC + "2m"
ORANGE = ESC + "38;2;215;119;87m"
YELLOW = ESC + "38;2;232;181;66m"
GREEN = ESC + "38;2;78;186;101m"
RED = ESC + "38;2;232;98;98m"
PURPLE = ESC + "38;2;177;151;252m" + ESC + "4m"
BAR = ESC + "48;2;55;55;55m"

LOGO = [" ▐▛███▜▌ ", "▝▜█████▛▘", "  ▘▘ ▝▝  "]
RELOAD_LABEL = " ↻ Reload (f5) "
RELOAD_EXIT = 3  # agents_mod.cmd reopens the program when it exits with this code

# Native Windows console reading (ReadConsoleInputW): arrows and mouse arrive
# as events, without depending on how the terminal encodes the key.
KEY_EVENT, MOUSE_EVENT = 0x1, 0x2
VK_ARROWS = {0x26: "H", 0x28: "P", 0x21: "I", 0x22: "Q", 0x25: "K", 0x27: "M", 0x2E: "S",   # S = Delete
             0x24: "G", 0x23: "O"}   # Home, End
ALT_ARROWS = {"H": "\x98", "P": "\xa0"}
SHIFT_ARROWS = {"K": "s", "M": "t", "H": "U", "P": "D"}
CTRL_ARROWS = {"H": "\x8d", "P": "\x91"}  # Ctrl+↑ / Ctrl+↓: scroll the side conversation  # Shift+← / Shift+→: move the card between Kanban columns


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", ctypes.c_int), ("wRepeatCount", ctypes.c_ushort),
                ("wVirtualKeyCode", ctypes.c_ushort), ("wVirtualScanCode", ctypes.c_ushort),
                ("uChar", ctypes.c_wchar), ("dwControlKeyState", ctypes.c_uint32)]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwMousePosition", COORD), ("dwButtonState", ctypes.c_uint32),
                ("dwControlKeyState", ctypes.c_uint32), ("dwEventFlags", ctypes.c_uint32)]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("MouseEvent", MOUSE_EVENT_RECORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", ctypes.c_ushort), ("Event", _EVENT)]
# Without the ✳: Windows Terminal draws it as a green emoji and pushes the name.
SPINNER = "·✢✶✻✽✻✶✢"
GROUPS = [("pinned", "Pinned"), ("needs", "Needs input"), ("working", "Working"), ("done", "Completed")]
QUEUE_AUTO_MAX = 10   # a queue bigger than this doesn't go out by itself (it was a paste, not you)
CHECK_TITLE ="✻ Finished · not seen yet"   # last group: finished and you haven't looked yet
MODEL_NAMES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}
# Shortcuts screen ("? Shortcuts" button or the ? key): what each key does, by view.
HELP = {
    "all": [
        ("ctrl+r", "rename the selected session · on one of your group titles, rename the group"),
        ("f2", "jump to the next session waiting for you"),
        ("shift+tab", "switch the selected session's mode (manual → accept edits → plan → auto)"),
        ("f5", "reload the program (picks up new code)"),
        ("ctrl+f", "search names, summaries and inside every conversation (enter jumps to the match)"),
        ("?", "show / hide this screen"),
        ("esc", "clear the text · quit the program"),
    ],
    "list": [
        ("↑ ↓", "pick a session · alt+↑↓ jumps between groups"),
        ("enter / →", "open the session (ctrl+z comes back) · on a title, collapse the group"),
        ("space", "reply to the selected session"),
        ("type + enter", "create a new session with the text"),
        ("ctrl+t · ctrl+p", "pin / unpin"),
        ("ctrl+g", "put in one of your groups (empty removes) · on a group title, ↑↓ move the whole group"),
        ("shift+↑ ↓", "move your group up / down"),
        ("ctrl+x 2x", "delete the session · on one of your group titles, ungroup"),
        ("shift+enter", "new line in the text (ctrl+j too)"),
        ("↑ ↓", "in an empty reply box: your previous prompts in that session"),
        ("alt+v", "paste the copied image into the text"),
    ],
    "kanban": [
        ("← → ↑ ↓", "pick a card"),
        ("type + enter", "send the message to the selected card"),
        ("ctrl+n", "create a new session instead of replying"),
        ("alt+v", "paste the copied image into the message"),
        ("tab", "queue the message: goes when the session finishes"),
        ("alt+↑", "edit the last queued message"),
        ("1 2 + enter", "answer pending questions and permissions with the option number"),
        ("ctrl+e", "show / hide tool output in the conversation"),
        ("ctrl+y", "copy the last ! command the session sent"),
        ("arrastar", "drag over the conversation to select text; it copies on release"),
        ("ctrl+l · ctrl+k", "open the localhost · open the session's latest artifact"),
        ("ctrl+u", "index of your prompts (or click ☰ index) · click a prompt or a ┤ on the bar to scroll there"),
        ("/", "show the session's commands · ↑↓ pick, tab completes"),
        ("ctrl+o", "show / hide the card's conversation next to the Kanban"),
        ("ctrl+↑ ↓", "scroll the side conversation 3 lines at a time"),
        ("pgup / pgdn", "scroll the side conversation half a screen at a time"),
        ("enter", "open the session (ctrl+z comes back)"),
        ("shift+← →", "move the card to another column (or drag it)"),
        ("ctrl+t · ctrl+p", "pin / unpin"),
        ("ctrl+g", "put in one of your groups · on a group column, ← → move the whole column"),
        ("shift+↑ ↓", "move your group column"),
        ("ctrl+x 2x", "delete the session"),
    ],
    "split": [
        ("↑ ↓", "pick a session · the conversation on the right follows"),
        ("type + enter", "reply to the selected session"),
        ("enter / →", "open the session (ctrl+z or ← comes back)"),
        ("ctrl+↑ ↓", "scroll the right conversation 3 lines at a time"),
        ("pgup / pgdn", "scroll the right conversation half a screen at a time"),
        ("ctrl+n", "create a new session instead of replying"),
        ("ctrl+t · ctrl+g", "pin · put in one of your groups"),
        ("alt+v", "paste the copied image into the message"),
        ("tab", "queue the message: goes when the session finishes"),
        ("alt+↑", "edit the last queued message"),
        ("1 2 + enter", "answer pending questions and permissions with the option number"),
        ("ctrl+e", "show / hide tool output in the conversation"),
        ("ctrl+y", "copy the last ! command the session sent"),
        ("arrastar", "drag over the conversation to select text; it copies on release"),
        ("ctrl+l · ctrl+k", "open the localhost · open the session's latest artifact"),
        ("ctrl+u", "index of your prompts (or click ☰ index) · click a prompt or a ┤ on the bar to scroll there"),
        ("/", "show the session's commands · ↑↓ pick, tab completes"),
        ("shift+↑ ↓", "move your group up / down"),
        ("ctrl+x 2x", "delete the session"),
    ],
    "focus": [
        ("← →", "previous / next session in the queue"),
        ("pgup / pgdn", "scroll the answer"),
        ("type + enter", "reply and move to the next one"),
        ("enter", "open the session (ctrl+z or ← comes back)"),
        ("ctrl+n", "create a new session instead of replying"),
        ("ctrl+x 2x", "delete the session"),
    ],
    "grid": [
        ("↑ ↓ ← →", "pick a panel"),
        ("space", "reply to the selected session"),
        ("enter", "open the session (ctrl+z comes back)"),
        ("ctrl+g", "put in one of your groups"),
        ("ctrl+x 2x", "delete the session"),
    ],
    "board": [
        ("←", "go to goals · type and press enter to create a goal"),
        ("type + enter", "create a task in the selected goal"),
        ("enter", "launch an agent on the task · if it has one, open it"),
        ("ctrl+a", "launch another agent on the same task"),
        ("space", "reply to the task's agent"),
        ("shift+→", "to Done marks it done"),
        ("ctrl+x 2x", "delete the task or goal (agents are kept)"),
    ],
}
HELP_LABEL = " ? Shortcuts "
FEAT_LABEL = " ⊞ Features "
# The features panel (button at the top): what can be turned on and off. All on by default;
# the choice is kept in config.json ("features": {key: false}). Rule: EVERY new feature goes
# here (one line in this list) and its code sits behind self.on("key").
FEATURES = [   # (key, name, what it is, panel area)
    ("summary", "SUMMARY", "summary at the top, by Haiku (costs tokens)", "Conversation"),
    ("prompt_time", "Prompt date and time", "on the right of your prompts", "Conversation"),
    ("code_copy", "Copy code", "🗐 at the end of each code block", "Conversation"),
    ("bang_buttons", "Copy ! commands", "click a ! command to copy it", "Conversation"),
    ("link_buttons", "Clickable links", "underlined links open in the browser", "Conversation"),
    ("file_buttons", "File buttons", "buttons for the files a message cites", "Conversation"),
    ("file_copy", "Copy file", "⧉ copy file: ctrl+v in Teams/WhatsApp attaches it", "Conversation"),
    ("md_browser", ".md in the browser", "formatted page; off opens the editor", "Conversation"),
    ("turn_line", "Working / Worked line", "✻ Working… / Worked for … at the end", "Conversation"),
    ("compact_bar", "Compact progress", "a progress bar while /compact runs", "Conversation"),
    ("background", "Subagents list", "one line per subagent running", "Conversation"),
    ("claude_p", "claude -p detection", "counts the claude -p a script started", "Conversation"),
    ("scroll_ticks", "Prompt marks", "┤ on the scrollbar and its tooltip", "Conversation"),
    ("drag_copy", "Drag to copy", "drag over the conversation, copies on release", "Conversation"),
    ("ctrl_y", "ctrl+y line", "the last ! command, ready to copy", "Bottom of the panel"),
    ("next_questions", "Next questions", "lists the questions still to come", "Bottom of the panel"),
    ("ctx_bar", "Context bar", "the /context bar by category", "Bottom of the panel"),
    ("mode", "Session mode", "⏵⏵ auto mode in the bottom line", "Bottom of the panel"),
    ("localhost", "Localhost", "▶ localhost in the bottom line", "Bottom of the panel"),
    ("artifacts", "Artifacts button", "⧉ N artifacts, opens the list", "Bottom of the panel"),
    ("folder", "Folder and branch", "📂 folder (click copies) and 🌿 branch", "Bottom of the panel"),
    ("cost", "Session cost", "💲 what the session would cost on the API", "Bottom of the panel"),
    ("list_columns", "List columns", "context %, cache, model and effort", "List and top"),
    ("plan_usage", "Plan usage", "5h / 7d usage at the top", "List and top"),
    ("check_group", "Finished · not seen yet", "group at the bottom and the blinking ✻", "List and top"),
    ("alert_clear", "Clear after 3 s", "'not seen' clears after 3 s on the session", "List and top"),
    ("sound", "Sound", "when a session needs you or finishes", "List and top"),
    ("auto_queue", "Auto-send the queue", "next queued message goes by itself", "Sending"),
    ("queue_pause", "Pause big queues", "a queue of more than 10 waits for you", "Sending"),
    ("paste_guard", "Paste protection", "enter and tab inside a paste don't send", "Sending"),
    ("click_log", "Click log", "clicks.log, to debug lost clicks", "Sending"),
]
SHORTCUTS = [
    ["ctrl+f to find", "shift+enter for newline", "esc to quit"],
    ["alt+↑/↓ to jump groups", "ctrl+p to pin", "? to close"],
    ["ctrl+x to delete", "space to reply", "enter to open"],
    ["f5 to reload", "ctrl+g to group", "tab to switch views"],
]


# ---------------------------------------------------------------- dados

_json_text = {}   # path → (file date and size, text): only rereads when it changes


def read_json(path, default):
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
        hit = _json_text.get(path)
        if hit is None or hit[0] != stamp:
            with open(path, encoding="utf-8-sig") as f:
                hit = _json_text[path] = (stamp, f.read())
        return json.loads(hit[1])   # new object each time: the reader may modify it
    except (OSError, ValueError):
        return default


def stamp(value, fallback):
    # updatedAt comes as ISO text ("2026-10-15T23:10:00.000Z") or a number in ms.
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            pass
    return fallback


_transcripts = {}


def transcript(session_id, state):
    path = state.get("linkScanPath")
    if path and os.path.exists(path):
        return path
    hit = _transcripts.get(session_id)
    if hit and os.path.exists(hit):
        return hit
    found = glob.glob(os.path.join(HOME, "projects", "*", f"{session_id}.jsonl"))
    if found:   # only caches what it found: a new session has no file yet
        _transcripts[session_id] = found[0]
        return found[0]
    return continued_from(session_id, state.get("cwd") or "")


_continued = {}   # session id → (time, conversation file it continued from)


def continued_from(session_id, cwd):
    """Session opened continuing another (--resume): until it writes its own file, the conversation is
    the old one's, which ends with {"type":"continued-in","continuedInSessionId": <this one>}."""
    hit = _continued.get(session_id)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    folder = os.path.join(HOME, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd)) if cwd else ""
    files = sorted(glob.glob(os.path.join(folder, "*.jsonl")), key=os.path.getmtime, reverse=True)[:15] if folder else []
    needle = f'"continuedInSessionId":"{session_id}'.encode()
    path = None
    for f in files:
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, os.path.getsize(f) - 200_000))
                if needle in fh.read():
                    path = f
                    break
        except OSError:
            continue
    _continued[session_id] = (time.time(), path)
    return path


_models = {}


def last_hit(path, pattern):
    # The model and effort of the last reply are near the end of the transcript: reads
    # backwards in 1 MB blocks (up to 32 MB) and caches the result while the
    # file doesn't change.
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        cached = _models.get((path, pattern.pattern))
        if cached and cached[0] == size:
            return cached[1]
        found, chunk, end = "", 1024 * 1024, size
        with open(path, "rb") as f:
            while end > 0 and size - end < 32 * chunk and not found:
                start = max(0, end - chunk)
                f.seek(start)
                hits = pattern.findall(f.read(end - start + 200))
                found = hits[-1].decode() if hits else ""
                end = start
        _models[(path, pattern.pattern)] = (size, found)
        return found
    except OSError:
        return ""


_activity = {}


def last_activity(path):
    """Time (ms) of the transcript's last message; cached while the file doesn't change."""
    if not path:
        return None
    try:
        size = os.path.getsize(path)
        cached = _activity.get(path)
        if cached and cached[0] == size:
            return cached[1]
        with open(path, "rb") as f:
            f.seek(max(0, size - 2 * 1024 * 1024))
            lines = f.read().splitlines()
        found = None
        for line in reversed(lines):
            if b'"timestamp"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            # Real messages only: system lines (resume, summary) don't count.
            if entry.get("type") not in ("user", "assistant") or entry.get("isMeta"):
                continue
            found = stamp(entry.get("timestamp"), None)
            if found:
                break
        _activity[path] = (size, found)
        return found
    except OSError:
        return None


_texts = {}


def last_text(path):
    """Text of Claude's last reply in the transcript (text blocks only)."""
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        cached = _texts.get(path)
        if cached and cached[0] == size:
            return cached[1]
        with open(path, "rb") as f:
            f.seek(max(0, size - 3 * 1024 * 1024))
            lines = f.read().splitlines()
        found = ""
        for line in reversed(lines):
            if b'"assistant"' not in line or b'"text"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "assistant":
                continue
            content = entry.get("message", {}).get("content")
            parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"] if isinstance(content, list) else []
            found = "\n".join(p for p in parts if p.strip()).strip()
            if found:
                break
        _texts[path] = (size, found)
        return found
    except OSError:
        return ""


def session_text(s, state):
    for i, sid in enumerate((s.get("sessionId", ""), state.get("resumeSessionId"), state.get("sessionId"))):
        if sid:
            found = last_text(transcript(sid, state if i == 0 else {}))
            if found:
                return found
    return ""


def session_path(s, state):
    """The session's conversation file (.jsonl), the same one the open session keeps writing."""
    for i, sid in enumerate((s.get("sessionId", ""), state.get("resumeSessionId"), state.get("sessionId"))):
        if sid:
            path = transcript(sid, state if i == 0 else {})
            if path and os.path.exists(path):
                return path
    return ""


def background_label(f, turns):
    """Readable name for something running in the background: the description Claude gave the command
    (the "description" of the Bash call that started it) or, without it, the command stripped of cd, nohup,
    variables and redirections, with paths reduced to the file name."""
    label = " ".join(str(f.get("label") or "").split())
    if f.get("kind") != "shell":
        return label
    head = label.rstrip("…").strip()
    shells = [t for t in reversed(turns) if t[0] == "tool" and t[1] in ("Bash", "PowerShell")
              and isinstance(t[4], dict) and t[4].get("description")]
    # The background Bash result cites the id ("…in background with ID: b0hgh6y18");
    # without it, the whole command has to match (the start, "cd folder &&", is the same in many).
    hit = next((t for t in shells if f.get("id") and f["id"] in str(t[3] or "")), None) or \
        next((t for t in shells if head and " ".join(str(t[4].get("command") or "").split()).startswith(head)), None)
    if hit:
        return " ".join(str(hit[4]["description"]).split())
    cmd = label.rstrip("…")
    cmd = re.sub(r'^\s*cd\s+("[^"]*"|\S+)\s*(&&|;)\s*', "", cmd)
    cmd = re.sub(r"^(\s*\w+=(\"[^\"]*\"|\S+)\s*(;|&&)?\s*)+", "", cmd)    # F=... ; X=1
    cmd = re.sub(r"^\s*nohup\s+", "", cmd)
    cmd = re.sub(r"\s*\d?>>?&?\s*(\"[^\"]*\"|\S+)", "", cmd)              # > log, 2>&1
    cmd = re.sub(r"\s*&\s*$", "", cmd)
    cmd = re.sub(r'"?[A-Za-z]:[\\/][^"\s]*[\\/]([^"\\/\s]+)"?', r"\1", cmd)   # path → just the name
    return cmd.strip() or label


def tool_label(name, data):
    """Tool header as the session shows it: Bash(command), Read(file)…"""
    data = data if isinstance(data, dict) else {}
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt", "skill"):
        if data.get(key):
            return f"{name}({' '.join(str(data[key]).split())})"
    first = next((v for v in data.values() if isinstance(v, str) and v.strip()), "")
    return f"{name}({' '.join(first.split())})" if first else name


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, dict) and c.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return ""


PASTED = re.compile(r"</?pasted_content[^>]*>")
COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)
BASH_OUT = re.compile(r"<bash-stdout>(.*?)</bash-stdout>\s*<bash-stderr>(.*?)</bash-stderr>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def user_texts(entry):
    """Your texts in a conversation line: normal message, pasted text (which comes inside
    <pasted_content>) and a message that arrived mid-work (queued_command)."""
    if entry.get("isMeta") or entry.get("isSidechain"):
        return []
    att = entry.get("attachment")
    if isinstance(att, dict) and att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
        texts = [att["prompt"]]
    elif entry.get("type") == "system" and entry.get("subtype") == "local_command":
        # Slash command in a background session: recorded as a system line.
        content = entry.get("content")
        texts = [content] if isinstance(content, str) and content.startswith("<command-name>") else []
    elif entry.get("type") == "user":
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        else:
            texts = []
    else:
        return []
    out = []
    for text in texts:
        text = text.strip()
        if text.startswith("<pasted_content"):
            text = PASTED.sub("", text).strip()
        bang = BASH_INPUT.search(text) if text.startswith("<bash-input>") else None
        if bang:   # the "! command" you sent
            text = "! " + bang.group(1).strip()
        name = COMMAND_NAME.search(text) if text.startswith("<command-") else None
        if name:   # slash command: recorded as <command-name>/x</command-name><command-args>…
            args = COMMAND_ARGS.search(text)
            text = (name.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()
        if text and not text.startswith("<") and not text.startswith("This session is being continued"):
            out.append(text)
    return out


def live_turns(path, limit=60):
    """Conversation read straight from the file the session writes while working:
    ("you", text), ("claude", text) or ("tool", name, header, result, input).
    The result stays None while the tool hasn't finished (a question waiting for you)."""
    try:
        stat = os.stat(path)
    except (OSError, TypeError):
        return []
    # Normally only the last 8 MB; when jumping to an old prompt via the index, reads from
    # it (_window) and stays on that stretch while you read: rereading 100 MB on every
    # session write would freeze the screen.
    start = _window.get(path)
    key = (stat.st_mtime, stat.st_size, start)
    # One at a time: the screen and the background preload read the same file, and two
    # reading the same new chunk duplicated the conversation.
    with _live_lock:
        cached = _live.get(path)
        if cached and (cached[0] == key or (start is not None and cached[0][2] == start)):
            return cached[1][-limit:]
        inc = _inc.get(path)
        if start is None and inc and inc["offset"] <= stat.st_size:
            # Only the new chunk: rereading 8 MB on every line the session writes froze the screen.
            try:
                with open(path, "rb") as f:
                    f.seek(inc["offset"])
                    chunk = f.read()
            except OSError:
                return inc["turns"][-limit:]
            cut = chunk.rfind(b"\n") + 1   # a half-written line is left for next time
            if cut:
                # split("\n"), not splitlines(): pasted text may contain   and other
                # separators inside the json, and splitlines cut the line in the middle.
                _parse_turns(chunk[:cut].decode("utf-8", "replace").split("\n"), inc["turns"], inc["tools"], inc["meta"])
                inc["offset"] += cut
            turns, meta = inc["turns"], inc["meta"]
        else:
            try:
                with open(path, "rb") as f:
                    pos = max(0, stat.st_size - 8_000_000) if start is None else max(0, min(start, stat.st_size - 8_000_000))
                    f.seek(pos)
                    data = f.read()   # the file may have grown since the stat: pos + what was read is what counts
            except OSError:
                return []
            cut = data.rfind(b"\n") + 1
            lines = data[:cut].decode("utf-8", "replace").split("\n")
            if pos > 0:
                lines = lines[1:]   # started in the middle of a line
            turns, tools, meta = [], {}, {"prompts": 0}
            _parse_turns(lines, turns, tools, meta)
            if start is None:
                _inc[path] = {"offset": pos + cut, "turns": turns, "tools": tools, "meta": meta}
        # Keeps everything it read and returns only the requested tail (the index asks for more when jumping back).
        _live[path] = (key, turns)
        _meta[path] = meta
        return turns[-limit:]


_live_lock = threading.Lock()


_inc = {}   # path → reading in progress: how far it has read, conversation, tools, data


def _parse_turns(raw, turns, tools, meta):
    """Joins the conversation file lines into turns (continues where it left off)."""
    for line in raw:
        if len(line) > 150_000:   # tool result with an image
            if '"tool_result"' in line:
                m = re.search(r'"tool_use_id":"([^"]+)"', line)
                if m and m.group(1) in tools:
                    i = tools[m.group(1)]
                    turns[i] = turns[i][:3] + ("[image]",) + turns[i][4:]
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isMeta") or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            # /compact (or automatic compaction): one line in the conversation, and the context
            # becomes what was left, until the next reply brings the new number.
            cm = entry.get("compactMetadata") or {}
            pre, post = cm.get("preTokens"), cm.get("postTokens")
            meta["compacted"] = entry.get("timestamp") or ""
            meta["compacting"] = None
            meta["fresh_compact"] = True   # until the next reply, the saved /context bar is the old one
            if pre and cm.get("durationMs"):
                # How long this session takes per token: the next bar estimates its time with it.
                meta["compact_rate"] = cm["durationMs"] / 1000 / pre
            if post:
                meta["used"] = post
            turns.append(("claude", "*── conversation compacted" + (
                f" · {tokens_short(pre)} → {tokens_short(post)}" if pre and post else "") + " ──*"))
            continue
        if entry.get("type") == "assistant" and isinstance(message.get("usage"), dict):
            # Same calculation as the status-line mod: context of the last reply, cache and model.
            u = message["usage"]
            meta["compacting"] = None   # Claude replied: no longer compacting
            if meta.get("fresh_compact"):
                # First reply after compaction: the /context saved after it is valid again.
                meta["compact_reply"] = entry.get("timestamp") or ""
            meta["fresh_compact"] = False
            meta["used"] = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            meta["ts"] = entry.get("timestamp") or meta.get("ts")
            meta["model"] = message.get("model") or meta.get("model")
            cc = u.get("cache_creation") or {}
            if cc.get("ephemeral_1h_input_tokens"):
                meta["ttl"] = 3600
            elif cc.get("ephemeral_5m_input_tokens"):
                meta["ttl"] = 300
        if entry.get("type") == "user" and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in tools:
                    i = tools[c["tool_use_id"]]
                    turns[i] = turns[i][:3] + (result_text(c.get("content")), turns[i][4], bool(c.get("is_error")))
        texts = user_texts(entry)
        if texts and meta.get("compacted") and (entry.get("timestamp") or "") < meta["compacted"]:
            texts = []   # after compaction Claude Code rewrites the "/compact": it is already in the conversation
        if texts:
            # "/compact" requested: compacting from now until the compaction mark arrives.
            # Any other message of yours after it (compaction cancelled) turns the bar off.
            first = texts[0].strip()
            compacting = first == "/compact" or first.startswith("/compact ")
            meta["compacting"] = entry.get("timestamp") if compacting else None
            if compacting:
                meta["compact_from"] = meta.get("used") or 0   # the context about to be compacted
        if texts and entry.get("type") == "user" and not texts[0].startswith("! "):
            # A request from you starts a new turn (the "! command" and the mid-work message don't).
            meta["turn_start"], meta["turn_tokens"], meta["turn_end"] = entry.get("timestamp"), 0, None
            meta["turn_done"] = None
        if entry.get("type") == "system" and entry.get("subtype") == "turn_duration":
            # Claude's turn is over (even with a command still running in the background).
            meta["turn_done"] = entry.get("timestamp") or True
        if entry.get("type") == "user" and isinstance(entry.get("permissionMode"), str):
            meta["mode"] = entry["permissionMode"]
        if entry.get("type") == "assistant" and isinstance(message.get("usage"), dict):
            meta["turn_tokens"] = (meta.get("turn_tokens") or 0) + (message["usage"].get("output_tokens") or 0)
            meta["turn_end"] = entry.get("timestamp")
            meta["turn_done"] = None   # back to work (e.g. the background command finished and woke it)
        if texts:
            for text in texts:
                turns.append(("you", text, entry.get("timestamp")))   # the time shows in the bubble
                meta["prompts"] += 1
        stdout = entry.get("content") if entry.get("subtype") == "local_command" else None
        if isinstance(stdout, str) and stdout.startswith("<local-command-stdout>"):
            # What the slash command replied (/context, /cost, mod command…).
            text = ANSI.sub("", re.sub(r"</?local-command-stdout>", "", stdout)).strip("\n")
            if text.strip() and text.strip() != "(no content)":   # /exit replies "(no content)"
                turns.append(("claude", "```\n" + text.replace("```", "'''") + "\n```"))
        if entry.get("type") == "user" and "<bash-stdout>" in str(content):
            raw = content if isinstance(content, str) else " ".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict))
            out = BASH_OUT.search(raw)
            if out:
                cmd = next((t[1][2:] for t in reversed(turns) if t[0] == "you" and t[1].startswith("! ")), "")
                text = (out.group(1) + ("\n" + out.group(2) if out.group(2).strip() else "")).strip() if out else ""
                turns.append(("tool", "Bash", tool_label("Bash", {"command": cmd}), text or "(no output)",
                              {"command": cmd}, bool(out and out.group(2).strip())))
        elif entry.get("type") == "assistant" and isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    turns.append(("claude", c["text"].strip()))
                elif c.get("type") == "tool_use":
                    name = c.get("name", "tool")
                    data = c.get("input") or {}
                    tools[c.get("id")] = len(turns)
                    turns.append(("tool", name, tool_label(name, data), None, data, False))


IMAGE_TAG = re.compile(r"\[Imag(?:em|e) #(\d+): [^\]]*\]")
_prompts = {}   # path → (bytes already read, prompts, byte where each one starts)
_window = {}    # path → byte the conversation is read from (index jump backwards)


_rename_lock = threading.Lock()
ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")


class Attached:
    """A live session opened through a hidden terminal (claude attach). It stays
    open while you type, so Enter sends right away."""

    def __init__(self, row):
        from winpty import PtyProcess
        import pyte
        self.id, self.row, self.out, self.cut, self.used = row["id"], row, [], 0, time.time()
        self.lock, self.ready, self.dead = threading.Lock(), threading.Event(), threading.Event()
        # Copy of the session screen: the open question/permission is read from it.
        self.screen, self.slock = pyte.Screen(160, 45), threading.Lock()
        self.version, self.cached = 0, (-1, [])
        self.stream = pyte.Stream(self.screen)
        self.p = PtyProcess.spawn(f"claude attach {row['id']}", dimensions=(45, 160), cwd=row.get("cwd") or None)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._warm, daemon=True).start()

    def _read(self):
        while not self.dead.is_set():
            try:
                chunk = self.p.read(65536)
            except Exception:
                break
            self.out.append(chunk)
            with self.slock:
                try:
                    self.stream.feed(chunk)
                    self.version += 1
                except Exception:
                    pass
            if len(self.out) > 400:
                with self.slock:
                    del self.out[:200]
                    self.cut += 200
        self.dead.set()

    def _mark(self):
        return self.cut + len(self.out)

    def _seen(self, mark, since):
        return mark in ANSI.sub("", "".join(self.out[max(0, since - self.cut):]))

    def lines(self):
        """The session screen, only the conversation part (the right column is another panel)."""
        with self.slock:
            if self.cached[0] != self.version:
                self.cached = (self.version, [l[:100].rstrip() for l in self.screen.display])
            return list(self.cached[1])

    def dialog(self):
        """Lines of the question or permission request open in the session; [] if none."""
        lines = self.lines()
        marks = ("Esc to cancel", "Enter to select", "Submit answers", "Do you want to proceed")
        end = next((i for i in range(len(lines) - 1, -1, -1) if any(m in lines[i] for m in marks)), None)
        if end is not None:   # the options right below the marker (e.g. "2. Cancel")
            while end + 1 < len(lines) and re.match(r"\s*(❯\s*)?\d+\. ", lines[end + 1]):
                end += 1
        if end is None:
            return []
        # The input box right below (❯ without an option number): the marker was conversation text
        # (e.g. the output of a command that quotes "Esc to cancel"), not an open window.
        if any(re.match(r"\s*❯(?!\s*\d+\.)", l) for l in lines[end + 1:]):
            return []
        # The start is the ─── line above the window. The question (AskUserQuestion) has another ───
        # in the middle, before "5. Chat about this" (or "Chat about this", unnumbered, when the
        # options sit next to a preview): that one doesn't count.
        rule = lambda i: lines[i].startswith("─" * 30)
        option = re.compile(r"\s*(❯\s*)?(\d+\. |Chat about this|Type something)")
        start = -1
        for i in range(end, -1, -1):
            if rule(i):
                after = next((l for l in lines[i + 1:end + 1] if l.strip()), "")
                if option.match(after):
                    continue
                start = i
                break
        return [l for l in lines[start + 1:end + 1] if l.strip() and not l.startswith("─" * 30)]

    def mode(self):
        """The mode the session shows at the bottom: "auto mode", "plan mode", "accept edits"…"""
        for line in reversed(self.lines()[-8:]):
            m = MODE_RE.search(line)
            if m:
                return m.group(2).strip()
            if "? for shortcuts" in line:
                return "manual mode"
        return ""

    def waiting_you(self):
        """The session says it has a question or permission open (even if the screen doesn't show it)."""
        return bool(self.row.get("questions") or self.row.get("permission"))

    def _box(self):
        """The text of the input box (between the last two ─── lines); None without a box."""
        lines = self.lines()
        rules = [i for i, l in enumerate(lines) if l.startswith("─" * 30)]
        if len(rules) < 2:
            return None
        return " ".join(l.strip() for l in lines[rules[-2] + 1:rules[-1]])

    def _refocus(self):
        """Esc gives the keyboard back to the box (focused mod panel) and ctrl+u clears the line. Only with
        the session idle: while working, esc would interrupt its work."""
        if self.row.get("busy"):
            return False
        self.p.write("\x1b")
        time.sleep(0.4)
        self.p.write("\x15")
        time.sleep(0.4)
        return True

    def _clear_box(self):
        """Clears the whole input box. ctrl+u only clears the cursor's line: with long
        text (more than one line in the box) the start was left, and the next paste went after it."""
        for _ in range(12):
            box = self._box()
            if not box or not box.replace("❯", "").strip():
                return
            self.p.write("\x15")
            time.sleep(0.12)
            if self._box() == box:   # ctrl+u changed nothing: the cursor is at the start of the line
                self.p.write("\x08")   # backspace joins it with the line above
                time.sleep(0.12)
                if self._box() == box:   # still nothing: it is the empty box's placeholder text
                    return

    def press(self, key):
        """One key for the question/permission (the option number)."""
        with self.lock:
            if self.ready.wait(5) and not self.dead.is_set():
                try:
                    self.p.write(key)
                except Exception:   # "Pty is closed": the connection died without warning
                    self.dead.set()
                    return False
                self.used = time.time()
                return True
            return False

    def _starting(self):
        """The attach is still waking the session (shows only the conversation, without the real box)."""
        return any("Session is starting" in l for l in self.lines()[-4:])

    def _warm(self):
        # The box (❯) shows up right away, but keys only count about 0.7 s later, and
        # that varies. Pastes a unique marker until it appears on screen; then clears it.
        # With a question/permission open it types nothing: a number there picks an option.
        limit = time.time() + (12 if self.row.get("alive") else 90)   # closed: attach reopens it first
        while time.time() < limit and not self.dead.is_set():
            if self.dialog() or (self.waiting_you() and "❯" in ANSI.sub("", "".join(self.out))):
                time.sleep(0.8)
                self.ready.set()
                return
            # Closed session waking up: attach shows the conversation with a decorative ❯ box
            # ("Session is starting — showing its transcript") until it comes up, which can take
            # over 15 s. That box takes no keys: only count the ❯ after the notice goes away.
            if "❯" in ANSI.sub("", "".join(self.out)) and not self._starting():
                break
            time.sleep(0.05)
        limit, tries = time.time() + 15, 0
        letters = "qzxkwjvghb"
        while time.time() < limit and not self.dead.is_set():
            if self.dialog() or self.waiting_you():
                self.ready.set()
                return
            tries += 1
            # Letters-only marker (no digits, no s/n): never picks any option.
            probe = "qz" + "".join(letters[int(c)] for c in f"{os.getpid() % 1000}{tries}") + "q"
            n = self._mark()
            self.p.write("\x1b[200~" + probe + "\x1b[201~")
            end = time.time() + 0.25
            while time.time() < end and not self._seen(probe, n):
                time.sleep(0.02)
            if self._seen(probe, n):
                self.p.write("\x15")   # ctrl+u: clears the line (the test markers)
                # Right after the first key the session still drops input: short grace period.
                time.sleep(0.4)
                if probe in (self._box() or "").replace(" ", ""):
                    self._refocus()   # ctrl+u didn't arrive: the keyboard is on a side panel
                self.ready.set()
                return
        self.close()

    def send(self, text, wait=15):
        """Pastes and presses Enter. Returns (True, "") if the message entered the conversation,
        (False, reason) if it didn't even type, (None, reason) if it typed without confirming."""
        with self.lock:
            if not self.ready.wait(wait) or self.dead.is_set():
                return False, "the session did not open in time"
            # Both markers together, in the box and in the footer: the same text quoted in a conversation doesn't count.
            tail = [l.strip() for l in self.lines()[-6:]]
            if "❯ describe a task for a new session" in tail and any(l.startswith("enter to return · space to reply") for l in tail):
                # The connection left the session and is on the claude agents list: the text would become
                # a new session. Closes; the caller opens another.
                self.close()
                return False, "the hidden connection had left the session"
            if self.dialog() or self.waiting_you():
                # Enter there would approve the highlighted option: types nothing.
                return False, "it is waiting on a question or permission: press the option number"
            path = self.row.get("transcript") or ""
            start = os.path.getsize(path) if path and os.path.exists(path) else 0
            first = text.strip().splitlines()[0]
            if first.startswith("!"):   # "! command" is recorded as <bash-input> command
                first = first[1:].strip()
            needle = json.dumps(first[:40], ensure_ascii=False)[1:-1].encode("utf-8")
            # What shows in the box: the start of the text (without spaces, which the screen
            # may wrap) or "Pasted text", when the text is large.
            head = "".join(text.split())[:12]

            def echoed(since):
                # The text has to be INSIDE the box (between the last two ───). Elsewhere on the
                # screen doesn't count: with the file panel focused, "/" opens its search
                # and the text went there; Enter sent nothing and "✓ sent" showed up.
                box = self._box()
                if box is not None:
                    box = "".join(box.split())
                    return head in box or "Pastedtext" in box
                shown = "".join(ANSI.sub("", "".join(self.out[max(0, since - self.cut):])).split())
                return head in shown or "Pastedtext" in shown

            def arrived():
                if path and os.path.exists(path) and os.path.getsize(path) > start:
                    with open(path, "rb") as f:
                        f.seek(start)
                        return needle in f.read()
                return False
            def box_empty():
                # The input box (between the last two ─── lines) no longer has the text: the
                # session accepted it. Works for the "! command", which is only recorded when it finishes.
                box = self._box()
                if box is None:
                    return False
                box = "".join(box.split())
                return head not in box and "Pastedtext" not in box

            for round_ in range(2):
                # 1) Paste until the text shows in the box (the session sometimes drops the paste).
                for paste_try in range(6):
                    if paste_try in (2, 4):
                        # Two pastes and nothing in the box: the keyboard is on a side panel (e.g.
                        # file tree search). Esc closes the search / gives focus back to the box.
                        self._refocus()
                    if paste_try:
                        self._clear_box()   # what's left of the previous paste can't stay in front
                    n = self._mark()
                    self.p.write("\x15\x1b[200~" + text + "\x1b[201~")
                    # A working session is slow to draw the box: waits longer before pasting again.
                    end = time.time() + (1.5 if self.row.get("busy") else 0.6)
                    while time.time() < end and not echoed(n):
                        time.sleep(0.02)
                    if echoed(n):
                        break
                else:
                    self.p.write("\x15")
                    return False, "the session did not accept the text"
                time.sleep(0.05)
                # 2) Enter until the message enters the conversation. Enter with an empty box does nothing.
                self.used = time.time()
                for attempt in range(5):
                    if attempt and self.dialog():   # a permission already opened: Enter would approve it
                        return True, ""
                    self.p.write("\r")
                    end = time.time() + (0.8 if attempt < 4 else 4)
                    while time.time() < end:
                        if arrived():
                            return True, ""
                        time.sleep(0.05)
                    if box_empty():
                        return True, ""
                # The text went in but Enter didn't: the session keyboard is on a side panel
                # (e.g. the file tree, after a click). Esc gives focus back to the box;
                # when idle, esc interrupts nothing. Clears what's left and tries once more.
                if round_ or not self._refocus():
                    break
            return None, ("typed it, but the session did not take the Enter: its keyboard may be on a side "
                          "panel (file tree); press esc inside it")

    def close(self):
        self.dead.set()
        try:
            self.p.terminate(force=True)
        except Exception:
            pass


def dialog_line(l, w):
    """A line of the question/permission copied from the session, with color: the highlighted one in orange,
    the question in bold, each option's explanation dimmed."""
    if any(m in l for m in ("Enter to select", "Esc to cancel")):
        return ""   # the session's key hint: the mod's stays in the title
    text = "  " + fit(l, w).rstrip()
    if re.match(r"\s*❯\s*\d+\. ", l):
        return ORANGE + BOLD + text + RESET
    if re.match(r"\s*\d+\. ", l):
        return text
    if l.startswith("←") or l.startswith("     "):
        return DIM + text + RESET
    return YELLOW + BOLD + text + RESET if l.rstrip().endswith("?") else text


_attached = {}
_attached_lock = threading.Lock()


def attach_open(row):
    """Opens (or reuses) the session's hidden terminal. Closes those idle for
    more than 2 min, and at most 3 stay open."""
    with _attached_lock:
        for key, conn in list(_attached.items()):
            if conn.dead.is_set() or not _safe(conn.p.isalive) or (key != row["id"] and time.time() - conn.used > 120):
                conn.close()
                _attached.pop(key, None)
        conn = _attached.get(row["id"])
        if conn is None:
            while len(_attached) >= 3:
                old = min(_attached, key=lambda k: _attached[k].used)
                _attached.pop(old).close()
            conn = _attached[row["id"]] = Attached(row)
        conn.row, conn.used = row, time.time()
        return conn


def attach_send(row, text):
    try:
        conn = attach_open(row)
    except ImportError:
        return False, "pywinpty is missing (py -3.11 -m pip install pywinpty)"
    except Exception as exc:
        return False, f"could not open the session: {exc}"
    # Closed session: attach reopens it (same id, same conversation), which takes a few seconds.
    try:
        return conn.send(text, wait=15 if row.get("alive") else 60)
    except Exception:
        # The hidden terminal died without warning (e.g. after cancelling the message, the
        # "Pty is closed"): discards it, opens another and sends again, once.
        with _attached_lock:
            if _attached.get(row["id"]) is conn:
                _attached.pop(row["id"], None)
        conn.close()
        try:
            conn = attach_open(row)
        except Exception as exc:
            return False, f"could not open the session: {exc}"
        return conn.send(text, wait=15 if row.get("alive") else 60)


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def attach_close_all():
    with _attached_lock:
        for conn in _attached.values():
            conn.close()
        _attached.clear()


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
PATH_QUOTED = re.compile(r"`([A-Za-z]:[\\/][^`\n]+?)`")          # between backticks it may contain spaces
PATH_BARE = re.compile(r"(?<![\w`])([A-Za-z]:[\\/][^\s`'\"<>|*?]+)")
_exists = {}


# What deserves a button at the end of the message: what you open to view (image, page, formatted
# text, video). Not code or folders: those you read in the editor, not here.
VIEW_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".html", ".htm", ".md",
            ".markdown", ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v",
            # Documents you send to someone (Excel, Word, PowerPoint, PDF). Without .xlsm, .xls,
            # .docm and .doc: those may carry macros.
            ".xlsx", ".csv", ".docx", ".pptx", ".pdf", ".txt")   # folders too (opens in Explorer)


def message_files(text, exts=None):
    """Files and folders the message cites that exist on this machine, in the order
    they appear (no repeats). "path:line" becomes just the path. With exts, only
    the folders and the files with those extensions."""
    found = []
    for m in list(PATH_QUOTED.finditer(text)) + list(PATH_BARE.finditer(text)):
        path = re.sub(r":\d+(?::\d+)?$", "", m.group(1).strip().rstrip(".,;:)]}"))
        if path in found:
            continue
        hit = _exists.get(path)
        if hit is None or time.time() - hit[1] > 5:
            hit = _exists[path] = (os.path.exists(path), time.time(), os.path.isdir(path))
        if hit[0] and (not exts or hit[2] or path.lower().endswith(exts)):
            found.append(path)
    return found[:8]


def prompt_text(text):
    """Prompt as it appears on screen: the pasted image becomes just [Imagem #n], without the path."""
    return IMAGE_TAG.sub(r"[Image #\1]", text)


_conv_texts = {}   # path → (bytes already read, prompts up to there, [(prompt number, who, text)])


def conversation_texts(path):
    """Your texts and Claude's, with the number of your prompt each one belongs to
    (same count as the index). Reads the file once and then only what the session wrote."""
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError):
        return []
    done, count, found = _conv_texts.get(path, (0, 0, []))
    if size < done:
        done, count, found = 0, 0, []
    if size == done:
        return found
    found = list(found)
    try:
        with open(path, "rb") as f:
            f.seek(done)
            chunk = f.read()
    except OSError:
        return found
    cut = chunk.rfind(b"\n") + 1
    for raw in chunk[:cut].split(b"\n"):
        if len(raw) > 150_000 or not raw:
            continue
        is_user = b'"type":"user"' in raw or b'"queued_command"' in raw or b'"local_command"' in raw
        if not is_user and b'"type":"assistant"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        for text in user_texts(entry):
            count += 1
            found.append((count, "you", text))
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            for c in (entry.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                    found.append((count, "claude", c["text"]))
    if len(_conv_texts) > 200:   # doesn't keep the conversation of a deleted session forever
        _conv_texts.pop(next(iter(_conv_texts)))
    _conv_texts[path] = (done + cut, count, found)
    return found


def search_conversations(rows, needle, per_session=60):
    """Where the word appears in each conversation: {id: [(prompt no., who, excerpt)]}."""
    needle = needle.lower()
    hits = {}
    for r in rows:
        found = []
        for n, who, text in conversation_texts(r.get("transcript")):
            low = text.lower()
            at = low.find(needle)
            if at < 0:
                continue
            flat = " ".join(text[max(0, at - 60): at + len(needle) + 120].split())
            found.append((max(1, n), who, flat))
        if found:
            hits[r["id"]] = found[-per_session:]
    return hits


def all_prompts(path):
    """All your prompts in the session, from the whole file (the live conversation only reads the
    last 8 MB). Reads once and then only what the session wrote; same filter as live_turns."""
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError):
        return []
    done, found, spots = _prompts.get(path, (0, [], []))
    if size < done:
        done, found, spots = 0, [], []
    if size == done:
        return found
    found, spots = list(found), list(spots)
    times = list(_prompt_times.get(path) or []) if done else []
    try:
        with open(path, "rb") as f:
            f.seek(done)
            chunk = f.read()
    except OSError:
        return found
    cut = chunk.rfind(b"\n") + 1   # a half-written line is left for the next read
    pos = done
    for raw in chunk[:cut].split(b"\n"):
        here, pos = pos, pos + len(raw) + 1
        if len(raw) > 150_000 or (b'"type":"user"' not in raw and b'"queued_command"' not in raw
                                  and b'"local_command"' not in raw):
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        for text in user_texts(entry):
            found.append(text)
            spots.append(here)
            times.append(entry.get("timestamp"))
    _prompts[path] = (done + cut, found, spots)
    _prompt_times[path] = times
    return found


_prompt_times = {}   # path → time of each prompt (same order as all_prompts)


def prompt_when(path, n):
    """"21/10 17:40" of when you sent prompt n (1 = the first); "" without a time."""
    times = _prompt_times.get(path) or []
    ms = stamp(times[n - 1], 0) if 0 < n <= len(times) and times[n - 1] else 0
    return datetime.fromtimestamp(ms / 1000).strftime("%d/%m %H:%M") if ms else ""


def prompt_offset(path, n):
    """Byte where prompt n (1 = the first) starts in the session file."""
    all_prompts(path)
    spots = _prompts.get(path, (0, [], []))[2]
    return spots[n - 1] if 0 < n <= len(spots) else None


_meta = {}
_slow = {}   # git / netstat results cached for a few seconds


def cached(key, seconds, fn):
    hit = _slow.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    try:
        value = fn()
    except Exception:
        value = None
    _slow[key] = (time.time(), value)
    return value


def quiet(argv, cwd=None):
    out = subprocess.run(argv, cwd=cwd or None, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=5, creationflags=0x08000000)
    return out.stdout if out.returncode == 0 else ""


def listening_ports():
    ports = set()
    for line in quiet(["netstat", "-ano", "-p", "tcp"]).splitlines():
        if "LISTENING" in line:
            tail = line.split()[1].rsplit(":", 1)[-1] if len(line.split()) > 1 else ""
            if tail.isdigit():
                ports.add(int(tail))
    return ports


def session_meta(path):
    """Context, cache and model of the last reply, read only from the end of the transcript
    (fast enough for all sessions on every refresh)."""
    try:
        stat = os.stat(path)
    except (OSError, TypeError):
        return {}
    key = (stat.st_mtime, stat.st_size)
    hit = _meta_tail.get(path)
    if hit and hit[0] == key:
        return hit[1]
    meta = {}
    try:
        with open(path, "rb") as f:
            f.seek(max(0, stat.st_size - 600_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        if '"usage"' not in line or '"type":"assistant"' not in line or len(line) > 150_000:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") or {}
        u = message.get("usage") or {}
        meta = {"used": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                + (u.get("cache_creation_input_tokens") or 0),
                "ts": entry.get("timestamp"), "model": message.get("model"),
                "ttl": 300 if (u.get("cache_creation") or {}).get("ephemeral_5m_input_tokens") else 3600}
        break
    _meta_tail[path] = (key, meta)
    return meta


_meta_tail = {}


USAGE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "status-usage")


_usage = [0.0, ""]


def plan_usage():
    if time.time() - _usage[0] < 5:
        return _usage[1]
    _usage[:] = [time.time(), _plan_usage()]
    return _usage[1]


def _plan_usage():
    """Plan usage (5h, 7d), written by the status-line mod of each open session
    (one file per session). An idle session keeps the number from its last reply, which
    goes stale; since usage only rises until the window resets, the newest window wins and,
    within it, the highest number."""
    best = {}
    now = time.time() * 1000
    try:
        names = os.listdir(USAGE_DIR)
    except OSError:
        names = []
    for name in names:
        data = read_json(os.path.join(USAGE_DIR, name), {})
        if not isinstance(data, dict) or now - (data.get("ts") or 0) > 1_800_000:
            continue
        for limit in data.get("limits") or []:
            if not isinstance(limit, dict) or not limit.get("label"):
                continue
            key = (limit.get("resetsAt") or 0, limit.get("pct") or 0)
            old = best.get(limit["label"])
            if old is None or key > ((old.get("resetsAt") or 0), old.get("pct") or 0):
                best[limit["label"]] = limit
    parts = []
    for label in sorted(best, key=lambda l: (l != "5h", l)):
        limit = best[label]
        pct = limit.get("pct") or 0
        color = GREEN if pct < 50 else YELLOW if pct < 80 else rgb("#E5534B")
        reset = limit.get("resetsAt")
        # 5h resets today (time only); 7d resets another day (weekday and time).
        if reset and limit.get("label") == "5h":
            when = f" {DIM}(resets {time.strftime('%H:%M', time.localtime(reset / 1000))}){RESET}"
        elif reset:
            at = time.localtime(reset / 1000)
            when = f" {DIM}(resets {('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[at.tm_wday]} {time.strftime('%H:%M', at)}){RESET}"
        else:
            when = ""
        parts.append(f"{color}{limit.get('label')}:{pct}%{RESET}{when}")
    return "  ".join(parts)


def context_window(model):
    """Model context window: 1M on 5.x and on anything running with [1m]; 200k on the others."""
    m = re.search(r"(opus|sonnet|haiku|fable)-(\d+)", model or "")
    if "[1m]" in (model or "") or (m and m.group(1) != "haiku" and int(m.group(2)) >= 5):
        return 1_000_000
    return 200_000


SUMMARIES_FILE = os.path.join(HOME, "jobs", "agents-summaries.json")
_summary_lock = threading.Lock()
_summary_running = set()


def session_brief(row, turns):
    """Summary of the whole session (what it is about and where it stands), made by Haiku from
    its requests and the last reply. It is cached and only redone when a new
    request arrives or the session changes state; until then, the last one holds."""
    asks = [t[1] for t in turns if t[0] == "you"]
    answer = next((t[1] for t in reversed(turns) if t[0] == "claude"), "")
    key = f"lang:{len(all_prompts(row.get('transcript'))) or len(asks)}:{row.get('group')}"   # lang: redo the old ones that were in English
    saved = read_json(SUMMARIES_FILE, {})
    hit = saved.get(row["id"]) if isinstance(saved, dict) else None
    if (not hit or hit.get("key") != key) and asks and row["id"] not in _summary_running:
        _summary_running.add(row["id"])
        threading.Thread(target=_make_brief, args=(row["id"], key, asks, answer, row.get("intent") or ""),
                         daemon=True).start()
    return (hit or {}).get("text", "")


def _make_brief(row_id, key, asks, answer, intent):
    try:
        pedidos = "\n".join("- " + " ".join(a.split())[:240] for a in ([intent] if intent else []) + asks[-14:])
        prompt = (f"User requests, first to last:\n{pedidos}\n\n"
                  f"Assistant's last reply:\n{' '.join(answer.split())[:900]}")
        # The summary comes out in the language the session is held in: without the
        # user's settings (their hooks changed the format) and with a system prompt for just that.
        out = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence", "--tools", "",
                              "--setting-sources", "project", "--system-prompt",
                              "You write one short sentence (max 30 words) summarizing what a work session is "
                              "about and where it stands now. Write it in the same language the user requests "
                              "are written in (Portuguese requests get a Portuguese sentence, with correct "
                              "accents). Reply with the sentence only, no quotes."],
                             input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=120, cwd=os.path.join(HOME, "jobs"), creationflags=0x08000000)
        text = " ".join(out.stdout.split()).strip('"') if out.returncode == 0 else ""
        if text:
            with _summary_lock:
                saved = read_json(SUMMARIES_FILE, {})
                saved = saved if isinstance(saved, dict) else {}
                saved[row_id] = {"key": key, "text": text}
                with open(SUMMARIES_FILE, "w", encoding="utf-8") as f:
                    json.dump(saved, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    finally:
        _summary_running.discard(row_id)


ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[\w-]+")


def session_artifacts(turns):
    """Links of the artifacts the session published (in the order they came out)."""
    found = []
    for t in turns:
        if t[0] == "tool" and t[1] == "Artifact" and t[3]:
            for url in ARTIFACT_URL.findall(t[3]):
                if url not in found:
                    found.append(url)
    return found


def artifact_titles(turns):
    """[(name, link)] of the session artifacts, most recent first. The name is the published
    title or the file name; republishing the same link only updates the name."""
    seen = {}
    for t in turns:
        if t[0] == "tool" and t[1] == "Artifact" and t[3]:
            data = t[4] if isinstance(t[4], dict) else {}
            name = data.get("title") or os.path.basename(str(data.get("file_path") or "")) or ""
            for url in ARTIFACT_URL.findall(t[3]):
                seen.pop(url, None)
                seen[url] = name or url.rsplit("/", 1)[-1]
    return [(name, url) for url, name in reversed(seen.items())]


def localhost_of(cwd):
    """Address of the folder's localhost, if it is up (the /localhost marker);
    "" if it is not; None if the folder has no .claude/localhost.json marker."""
    marker = read_json(os.path.join(cwd, ".claude", "localhost.json"), {}) if cwd else {}
    if isinstance(marker, dict) and marker:
        ports = cached("ports", 15, listening_ports) or set()
        rota = str(marker.get("path") or "").strip()
        rota = ("/" + rota.lstrip("/")).rstrip("/") if rota else ""
        if marker.get("django") in ports:
            return f"http://localhost:{marker['django']}{rota}"
        return ""
    return "" if cwd and os.path.exists(os.path.join(cwd, "manage.py")) else None


def status_short(row, now=None, model_w=0):
    """The status line in a short version, to fit in the list beside the conversation:
    context, cache, effort and branch."""
    now = now or time.time()
    meta = row.get("meta") or {}
    # Each part has a fixed width: without the data, the space stays, and the columns do not warp.
    ctx = cache = " " * 6
    if meta.get("used"):
        pct = meta["used"] * 100 / context_window(meta.get("model") or row.get("model"))
        dot = "🟢" if pct < 50 else "🟡" if pct < 80 else "🔴"
        ctx = f"{dot}{round(pct):>3}%"
    if meta.get("ts"):
        left = stamp(meta["ts"], 0) / 1000 + meta.get("ttl", 3600) - now
        cache = "🧊" + DIM + "  --" + RESET if left <= 0 else f"🧊{'<1' if left < 60 else int(left // 60):>3}m"
    # Emoji, like the 🧊 for cache: Windows draws it in color with its emoji font. Chip
    # does not exist as an emoji; 🧠 (model) and ⚡ (effort) are the same as in the status line.
    # "medium" becomes "med": at 6 letters it did not fit the column and came out as "medi…".
    effort = f"⚡ {'med' if row['effort'] == 'medium' else row['effort']}" if row.get("effort") else ""
    # Model at the width of the longest in the list, so the space between columns is always one.
    model = fit(model_label(row), model_w or text_width(model_label(row)))
    return f"{ctx} {cache} 🧠 {model} {effort}".rstrip()


CTX_DIR = os.path.join(HOME, "status-context")   # the status-line mod writes the /context of each session here
# Color of each /context category (by the start of the name, lowercase).
CTX_COLORS = [("system prompt", (110, 150, 220)), ("system tools", (100, 200, 195)), ("mcp", (165, 135, 250)),
              ("custom agents", (140, 205, 120)), ("agents", (140, 205, 120)), ("memory", (225, 200, 95)),
              ("skills", (235, 150, 200)), ("messages", (215, 110, 90))]
CTX_FREE = (52, 56, 66)
COMPACT_RATE = 38.5 / 577_000   # seconds per token in a /compact (measured: 577k took 38 s)


def tokens_short(n):
    return f"{n / 1_000_000:g}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n < 10_000 else f"{round(n / 1000)}k"


def compact_pct():
    """The % of the window at which Claude compacts, from its settings.json (env) or the environment; 0 with no setting."""
    raw = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") or \
        str(((cached("settings-env", 60, lambda: read_json(SETTINGS, {})) or {}).get("env") or {}).get(
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") or "")
    try:
        pct = float(raw)
    except ValueError:
        return 0
    return pct if 0 < pct < 100 else 0


def context_lines(row, width):
    """The context bar, as in /context: total, where it compacts, one colored bar per
    category and the legend. Comes from the file the status-line mod writes at the end of each turn of
    Claude; without it (session that has not yet finished once with the mod), it does not appear."""
    convo = os.path.basename(row.get("transcript") or "")[:-6]
    data = read_json(os.path.join(CTX_DIR, convo + ".json"), {}) if convo else {}
    if not isinstance(data, dict) or not data.get("max") or not data.get("rows"):
        return []
    total, top = data.get("total") or 0, data["max"]
    meta = row.get("meta") or _meta.get(row.get("transcript") or "") or {}
    # The saved /context only counts after the first reply following compaction: the mod may
    # rewrite the file right after /compact still holding the old numbers (569k instead of 15k).
    valid_from = max(stamp(meta.get("compacted"), 0), stamp(meta.get("compact_reply"), 0)) if meta.get("compacted") else 0
    if meta.get("compacted") and (meta.get("fresh_compact") or valid_from > (data.get("ts") or 0)):
        # Compacted after the last valid /context: the old categories no longer count.
        # Shows only what is left; the mod writes the new number at the end of the next reply.
        left = meta.get("used") or 0
        pct = round(left * 100 / top)
        head = (ORANGE + "◆ " + RESET + BOLD + tokens_short(left) + RESET + DIM + f"/{tokens_short(top)} " + RESET
                + GREEN + BOLD + f"{pct}%" + RESET + DIM + "   compacted · categories come back with the next reply" + RESET)
        n = max(1, round(left * width / top)) if left else 0
        return [fit_ansi(head, width), ESC + "38;2;150;150;150m" + "━" * n + ESC + f"38;2;{CTX_FREE[0]};{CTX_FREE[1]};{CTX_FREE[2]}m"
                + "━" * (width - n) + RESET]
    used = [r for r in data["rows"] if r.get("kind") == "used" and (r.get("tokens") or 0) > 0]
    free = sum(r.get("tokens") or 0 for r in data["rows"] if r.get("kind") == "free")
    color = lambda name: next((c for k, c in CTX_COLORS if name.lower().startswith(k)), (150, 150, 150))
    fg = lambda c: ESC + f"38;2;{c[0]};{c[1]};{c[2]}m"
    pct = round(total * 100 / top)
    # Where it compacts: your setting (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, e.g. 60% of the window) wins over
    # the Claude Code number, which ignores the setting and gives the window minus the reserve.
    pct_cfg = compact_pct()
    if pct_cfg:
        data["compactAt"] = round(top * pct_cfg / 100)
    # Only two lines: the total and the whole legend on one line (short names), and the bar below.
    short = lambda name: next((s for k, s in (("system prompt", "system"), ("system tools", "tools"),
                                               ("mcp", "mcp"), ("custom agents", "agents"), ("memory", "memory"),
                                               ("skills", "skills"), ("messages", "msgs"))
                               if name.lower().startswith(k)), name.lower())
    head = (ORANGE + "◆ " + RESET + BOLD + tokens_short(total) + RESET + DIM + f"/{tokens_short(top)} " + RESET
            + (GREEN if pct < 50 else YELLOW if pct < 80 else ESC + "38;2;232;98;98m") + BOLD + f"{pct}%" + RESET
            # "┃600k": the same yellow stroke as the bar, with where it is.
            + ("  " + YELLOW + "┃" + RESET + DIM + tokens_short(data["compactAt"]) + RESET if data.get("compactAt") else ""))
    merged = {}   # categories with the same short name become one (the two "mcp" lines)
    for r in used:
        c, n = merged.get(short(r["name"]), (color(r["name"]), 0))
        merged[short(r["name"])] = (c, n + r["tokens"])
    items = [(c, name, n) for name, (c, n) in merged.items()] + [(CTX_FREE, "free", free)]
    # "■ system 4.9k": a space between the square and the name; between items, two spaces
    # (just one when the line does not fit).
    cells = [fg(c) + "■" + RESET + f" {name} " + DIM + tokens_short(n) + RESET for c, name, n in items]
    for gap in ("   ", "  ", " "):   # the widest gap between items that still fits on the line
        legend = gap.join(cells)
        if visible_width(head) + 3 + visible_width(legend) <= width:
            break
    lines = [fit_ansi(head + "   " + legend, width)]
    # The bar: each category takes its share of the window; the rest is free space.
    cells = []
    for r in used:
        cells += [color(r["name"])] * max(1, round(r["tokens"] * width / top))
    cells = cells[:width] + [CTX_FREE] * max(0, width - len(cells))
    mark = round(data["compactAt"] * width / top) if data.get("compactAt") else None
    bar = ""
    for i, c in enumerate(cells):
        bar += (YELLOW + "┃") if mark is not None and i == min(width - 1, mark) else fg(c) + "━"
    lines.append(bar + RESET)
    return lines


def model_label(row):
    """Short model: "claude-opus-5-5" becomes "opus 5.5"."""
    meta = row.get("meta") or {}
    parts = re.findall(r"[a-z]+|\d+", (meta.get("model") or row.get("model") or "").replace("claude-", ""))
    words = [p for p in parts if not p.isdigit()][:1]
    nums = [p for p in parts if p.isdigit() and len(p) < 4][:2]
    return " ".join(words + [".".join(nums)] if nums else words) or "—"


# What the session would cost through the API, in US$ per million tokens: input, output and cache read
# (table from the claude-api skill, 06/Oct/2026). Writing to cache costs 1.25x the input
# (5 min) or 2x (1 h). The first prefix that matches wins, so the longest comes first.
PRICES = [("fable-5-1", 10, 50, 0.25), ("mythos-5-1", 10, 50, 0.25), ("fable", 10, 50, 1),
          ("mythos", 10, 50, 1), ("opus-5-5", 4, 20, 0.2), ("opus", 5, 25, 0.5),
          ("sonnet-5", 2, 10, 0.2), ("sonnet", 3, 15, 0.3), ("haiku-5-5", 0.1, 0.5, 0.01),
          ("haiku", 1, 5, 0.1)]
_cost = {}   # file → [how far it read, response ids already counted, total in US$]


def usage_cost(model, u):
    """Price of a response, from the tokens in its usage; None if the model is unknown."""
    price = next((p for p in PRICES if p[0] in (model or "")), None)
    if not price:
        return None
    _, inp, out, read = price
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens") or 0
    w5m = (u.get("cache_creation_input_tokens") or 0) - w1h
    prompt = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + w5m + w1h
    scale = 5 if price[0] == "haiku-5-5" and prompt > 100_000 else 1   # Haiku 5.5 above 100K
    scale *= 2 if u.get("speed") == "fast" else 1                      # fast mode: double price
    cost = ((u.get("input_tokens") or 0) * inp + (u.get("output_tokens") or 0) * out
            + (u.get("cache_read_input_tokens") or 0) * read + w5m * inp * 1.25 + w1h * inp * 2)
    searches = (u.get("server_tool_use") or {}).get("web_search_requests") or 0
    return cost * scale / 1e6 + searches * 0.01                       # search: US$ 10 per thousand


def session_cost(path):
    """Cost of the whole conversation and its subagents; reads only what the file grew by."""
    total = 0.0
    for p in [path] + glob.glob(os.path.join(path[:-6], "subagents", "*.jsonl")):
        state = _cost.setdefault(p, [0, set(), 0.0])
        try:
            size = os.path.getsize(p)
            if size < state[0]:
                state[:] = [0, set(), 0.0]
            with open(p, "rb") as f:
                f.seek(state[0])
                data = f.read(size - state[0])
        except OSError:
            continue
        cut = data.rfind(b"\n") + 1
        state[0] += cut
        for line in data[:cut].split(b"\n"):
            if b'"usage"' not in line or b'"assistant"' not in line:
                continue
            try:
                message = json.loads(line).get("message") or {}
            except ValueError:
                continue
            # Each block of the response becomes a line with the same id and the same usage: count it once.
            if not isinstance(message.get("usage"), dict) or message.get("id") in state[1]:
                continue
            state[1].add(message.get("id"))
            state[2] += usage_cost(message.get("model"), message["usage"]) or 0
        total += state[2]
    return total


def headless_agents(row):
    """How many "claude -p" (testers, batches) are working in the session folder or
    below it. A session script opens them on its own; Claude counts just "1 shell". Each
    one writes the conversation (entrypoint sdk-cli) in the projects folder of its folder:
    counts the writes in the last minute. Returns [{"label", "startedAt"}], one per "claude -p"."""
    cwd = row.get("cwd") or ""
    if not cwd:
        return []
    prefix = re.sub(r"[^A-Za-z0-9]", "-", cwd).lower()
    base = os.path.join(HOME, "projects")
    try:
        dirs = [d for d in os.listdir(base) if d.lower().startswith(prefix)]
    except OSError:
        return []
    own = os.path.normcase(row.get("transcript") or "")
    found, now = [], time.time()
    for d in dirs:
        for f in glob.glob(os.path.join(base, d, "*.jsonl")):
            try:
                if now - os.path.getmtime(f) > 60 or os.path.normcase(f) == own:
                    continue
                with open(f, "rb") as fh:
                    head = fh.read(20000)
                started = os.path.getctime(f)
            except OSError:
                continue
            if b'"entrypoint":"sdk-cli"' not in head:
                continue
            label = ""   # the start of the request the script gave it
            for line in head.split(b"\n"):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                texts = user_texts(entry) if isinstance(entry, dict) and entry.get("type") == "user" else []
                if texts:
                    label = " ".join(texts[0].split())
                    break
            found.append({"label": label or os.path.basename(f), "startedAt": started * 1000})
    return sorted(found, key=lambda h: h["startedAt"])


def open_detached(target):
    """Opens a file or link in the default app without giving it this screen: os.startfile makes the
    app inherit the console, and VS Code writes its log ("[main …] update#setState") over
    the drawing. Through Explorer, Explorer opens it, and the app is born away from this console."""
    if not os.path.exists(target) and not re.match(r"^[a-z][a-z0-9+.-]*:", str(target), re.I):
        raise OSError(f"not found: {target}")
    subprocess.Popen(["explorer", os.path.normpath(target) if os.path.exists(target) else target],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=0x08000000)


MD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title><base href="__BASE__">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5.5.1/github-markdown.min.css"
  integrity="sha384-e9Z5Y4NP8q09WWmORPPBa0O1ZwQbbp3DDIFLatAwPCB7uxLR46YRAGw/QI/mxsBj" crossorigin="anonymous">
<style>
  body { margin: 0; background: #ffffff; }
  @media (prefers-color-scheme: dark) { body { background: #0d1117; } }
  .bar { max-width: 980px; margin: 0 auto; padding: 14px 32px 0; font: 12px system-ui, sans-serif; color: #8b949e; word-break: break-all; }
  .markdown-body { box-sizing: border-box; max-width: 980px; margin: 0 auto; padding: 16px 32px 48px; }
  @media (max-width: 767px) { .markdown-body, .bar { padding-left: 16px; padding-right: 16px; } }
</style></head>
<body><div class="bar">__PATH__</div><article id="md" class="markdown-body"></article>
<!-- integrity: the browser refuses the CDN file unless it is exactly this one. -->
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"
  integrity="sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"
  integrity="sha384-+VfUPEb0PdtChMwmBcBmykRMDd+v6D/oFmB3rZM/puCMDYcIvF968OimRh4KQY9a" crossorigin="anonymous"></script>
<script>
  const src = __SOURCE__;
  const el = document.getElementById("md");
  try { el.innerHTML = DOMPurify.sanitize(marked.parse(src, { gfm: true })); }
  catch (e) { el.textContent = src; }   // sem internet: o texto puro
</script></body></html>
"""


def md_page(path):
    """A page of the .md file, formatted (headings, tables, code), to open in the
    browser. Relative images and links work: the page points to the .md folder."""
    import html
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    folder = os.path.join(DATA_DIR, "reading")
    os.makedirs(folder, exist_ok=True)
    name = re.sub(r"[^\w.-]", "_", os.path.basename(path))
    out = os.path.join(folder, name + ".html")
    import pathlib
    base = pathlib.Path(os.path.dirname(os.path.abspath(path))).as_uri().rstrip("/") + "/"
    page = (MD_PAGE.replace("__TITLE__", html.escape(os.path.basename(path)))
            .replace("__BASE__", html.escape(base))
            .replace("__PATH__", html.escape(path))
            # "</" becomes "<\/": the .md text does not close the <script> in the middle.
            .replace("__SOURCE__", json.dumps(text, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")))
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    return out


def cost_label(row):
    """"$1.23" of what the session would cost through the API, or "" without the conversation."""
    path = row.get("transcript") or ""
    if not path.endswith(".jsonl"):
        return ""
    cost = cached(("cost", path), 10, lambda: session_cost(path))
    return f"${cost:,.2f}" if cost < 1000 else f"${cost / 1000:,.1f}k"


def status_items(row, now=None):
    """The status-line mod status line, for the chosen session:
    context, cache, branch, effort, localhost, model and how many prompts."""
    now = now or time.time()
    meta = row.get("meta") or _meta.get(row.get("transcript") or "") or {}
    cwd = row.get("cwd") or ""
    items = []
    if meta.get("used"):
        pct = meta["used"] * 100 / context_window(meta.get("model") or row.get("model"))
        dot = "🟢" if pct < 50 else "🟡" if pct < 80 else "🔴"
        items.append(f"{dot} {round(pct)}% {DIM}({round(meta['used'] / 1000)}k){RESET}")
    if meta.get("ts"):
        left = stamp(meta["ts"], 0) / 1000 + meta.get("ttl", 3600) - now
        items.append("🧊 cache expirou" if left <= 0 else
                     f"🧊 cache {'<1' if left < 60 else int(left // 60)}m{' ⚠' if left < 300 else ''}")
    if cwd:
        branch = row.get("branch")
        if branch is None:
            branch = cached(("branch", cwd), 30, lambda: quiet(["git", "symbolic-ref", "--short", "HEAD"], cwd).strip())
        if branch:
            items.append(f"🌿 {branch}")
    if row.get("effort"):
        items.append(f"⚡ {row['effort']}")
    marker = read_json(os.path.join(cwd, ".claude", "localhost.json"), {}) if cwd else {}
    if isinstance(marker, dict) and marker:
        ports = cached("ports", 15, listening_ports) or set()
        rota = str(marker.get("path") or "").strip()
        rota = ("/" + rota.lstrip("/")).rstrip("/") if rota else ""
        if marker.get("django") in ports:
            items.append(f"http://localhost:{marker['django']}{rota}")
        if marker.get("vite") in ports:
            items.append(f"Vite:{marker['vite']}")
    elif cwd and os.path.exists(os.path.join(cwd, "manage.py")):
        items.append(DIM + "▶ localhost" + RESET)
    model = meta.get("model") or row.get("model")
    if model:
        m = re.search(r"(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?!\d)", model)
        items.append(f"{m.group(1).capitalize()} {m.group(2)}{'.' + m.group(3) if m.group(3) else ''}" if m else model)
    return items


_live = {}


def session_activity(s, state):
    # Same logic as the model: current transcript, or the previous one if the current is empty.
    for i, sid in enumerate((s.get("sessionId", ""), state.get("resumeSessionId"), state.get("sessionId"))):
        if sid:
            found = last_activity(transcript(sid, state if i == 0 else {}))
            if found:
                return found
    return None


def session_hit(s, state, pattern):
    # A resumed session starts a new transcript, still without a reply: in that case
    # what is in the previous transcript of the same session holds.
    found = last_hit(transcript(s.get("sessionId", ""), state), pattern)
    for older in (state.get("resumeSessionId"), state.get("sessionId")):
        if found or not older:
            break
        found = last_hit(transcript(older, {}), pattern)
    return found


def elapsed(state, group, fallback_ms):
    """Seconds for the right column, by the `claude agents` rule (checked on
    16/10/2026 against the 15 sessions on the original screen):
    done = how long it took to finish the first time (firstTerminalAt -
    createdAt); working or waiting for you = how long ago it was created."""
    created = stamp(state.get("createdAt"), fallback_ms) / 1000
    first_done = stamp(state.get("firstTerminalAt"), 0) / 1000
    if group == "done" and first_done:
        return max(0, first_done - created)
    return max(0, time.time() - created)


def duration(seconds):
    # Unit written out in full: "2m" left doubt between minute and month.
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    days = s // 86400
    return f"{days} day" if days == 1 else f"{days} days"


def tokens_label(n):
    if n < 1000:
        return f"{n or 0} tokens"
    return f"{n / 1_000_000:.1f} mi tokens" if n >= 1_000_000 else f"{round(n / 1000)} mil tokens"


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32), ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260)]


def descendants(root):
    """{pid: exe name} of all processes below root (quick Windows snapshot)."""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    parents, names = {}, {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
    while ok:
        parents[entry.th32ProcessID] = entry.th32ParentProcessID
        names[entry.th32ProcessID] = entry.szExeFile
        ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    k32.CloseHandle(ctypes.c_void_p(snap))
    found, todo = {}, [root]
    while todo:
        pid = todo.pop()
        for child, parent in parents.items():
            if parent == pid and child not in found and child != root:
                found[child] = names[child]
                todo.append(child)
    return found


def timeline(job_id, limit=400):
    """History of the session (~/.claude/jobs/<id>/timeline.jsonl): each line is a
    request from you (empty text, outside working), a Claude step (working) or a
    reply (text with what it wrote)."""
    path = os.path.join(HOME, "jobs", job_id, "timeline.jsonl")
    try:
        mtime = os.path.getmtime(path)
        cached = _timelines.get(path)
        if cached and cached[0] == mtime:   # the pop-up redraws several times per second
            return cached[1]
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 600_000))
            lines = f.read().decode("utf-8", "replace").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entry["ms"] = stamp(entry.get("at"), 0)
            out.append(entry)
    _timelines[path] = (mtime, out)
    return out


_timelines = {}


def agents_json():
    out = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True, encoding="utf-8",
                         timeout=30)
    return json.loads(out.stdout or "[]")


def snapshot():
    now = time.time() * 1000
    rows = []
    for s in agents_json():
        if not s.get("id"):   # session just being born, still without an id: it enters on the next round
            continue
        state = read_json(os.path.join(HOME, "jobs", s["id"], "state.json"), {})
        output = state.get("output") if isinstance(state.get("output"), dict) else {}
        # claude agents wins over state.json: after you approve, state.json
        # may stay at "blocked" with the session already working (and it vanished as "needs you").
        flow = s.get("state") or state.get("state") or ""
        busy = s.get("status") == "busy"
        block = state.get("block") if isinstance(state.get("block"), dict) else {}
        questions = [q for q in block.get("questions") or [] if isinstance(q, dict) and q.get("question")]
        if questions:   # it is stopped on a question for you (AskUserQuestion)
            flow = "blocked"
        # Open question or permission request: state.json stays "working", but
        # claude agents says "waiting"/"blocked".
        if s.get("status") == "waiting" or s.get("state") == "blocked":
            flow = "blocked"
        # Stopped process ("idle") with state.json still "working": the turn ended and the
        # state went stale (e.g. the session opened another session and did not update its own).
        if s.get("status") == "idle" and flow == "working":
            flow = "done"
        needs = state.get("needs") if isinstance(state.get("needs"), str) else ""
        permission = needs[len("approve "):] if flow == "blocked" and needs.startswith("approve ") else ""
        if flow in ("blocked", "done"):
            # Waiting for you or finished: the process may stay "busy" because of tasks
            # in the background (running command, artifact watcher), but the turn ended.
            busy = False
        group = "needs" if flow == "blocked" else "working" if (flow == "working" or busy) else "done"
        if group == "working" and (_meta.get(session_path(s, state)) or {}).get("turn_done"):
            # claude agents keeps saying "working" while a command runs behind the scenes, but
            # Claude's turn is over (it comes back on its own when the command finishes).
            group, busy = "done", False
        inflight = state.get("inFlight") if isinstance(state.get("inFlight"), dict) else {}
        background = max(0, (inflight.get("tasks") or 0) - (inflight.get("drainableMonitors") or 0))
        if group == "done" and background and s.get("pid"):
            # Subagent or command still running behind the scenes: Claude comes back when it finishes,
            # so it is not done (it showed as ready while the work went on).
            group, busy = "working", True
        summary = (state.get("detail") if group == "working" else None) or output.get("result") or state.get("detail") or ""
        children = state.get("children") or []
        path = session_path(s, state)
        rows.append({
            "meta": session_meta(path),
            "id": s["id"],
            "sessionId": state.get("sessionId") or s.get("sessionId", ""),
            "name": s.get("name") or state.get("name") or s["id"],
            "cwd": s.get("cwd") or state.get("cwd") or "",
            "group": group,
            "alive": bool(s.get("pid")),
            "busy": busy or group == "working",
            "permission": permission,   # "PowerShell: command" when it asks for your permission
            "summary": " ".join(str(summary).split()),
            "model": session_hit(s, state, MODEL),
            "effort": session_hit(s, state, EFFORT),
            "last_text": session_text(s, state),
            "transcript": session_path(s, state),
            "branch": cached(("branch", s.get("cwd") or state.get("cwd") or ""), 30,
                             lambda: quiet(["git", "symbolic-ref", "--short", "HEAD"],
                                           s.get("cwd") or state.get("cwd") or None).strip()),
            "updated": session_activity(s, state) or stamp(state.get("updatedAt"), s.get("startedAt") or now),
            "elapsed": elapsed(state, group, s.get("startedAt") or now),
            "prs": [c.get("id", "") for c in children if c.get("kind") == "pr"],
            "frames": sum(1 for c in children if c.get("kind") == "frame"),
            "tokens": state.get("tokens") if isinstance(state.get("tokens"), int) else 0,
            # Commands still running behind the scenes (the artifact watcher does not count): "2 shells still running".
            "background": background,
            # What runs behind the scenes, with type (agent = subagent, shell = command), name and start.
            "fan": [f for f in state.get("fan") or [] if isinstance(f, dict) and f.get("kind")],
            # Token pasted in the request (Authorization: Bearer …) does not show on screen.
            "intent": re.sub(r"(?i)(bearer\s+|token[=:]\s*)\S+", r"\1•••",
                             " ".join(str(state.get("intent") or "").split())),
            "questions": questions,
        })
    rows.sort(key=lambda r: -r["updated"])
    return rows


def header_info():
    settings = read_json(SETTINGS, {})
    model = MODEL_NAMES.get(settings.get("model", ""), settings.get("model") or "claude-opus-5-5")
    effort = (settings.get("modelSettings", {}).get(model, {}) or {}).get("effortLevel") or settings.get("effortLevel") or "medium"
    parts = model.replace("claude-", "").split("-")
    pretty = parts[0].capitalize() + " " + ".".join(parts[1:]) if len(parts) > 1 else model
    try:
        version = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=15).stdout.split()[0]
    except (OSError, IndexError, subprocess.TimeoutExpired):
        version = "?"
    return version, pretty, effort


# ---------------------------------------------------------------- text

def cell_width(c):
    if unicodedata.combining(c):
        return 0
    # Symbols the terminal draws as emoji (2 columns) even though they are "N" in Unicode.
    if c in WIDE_SYMBOLS:
        return 2
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


WIDE_SYMBOLS = set("☰☱☲☳☴☵☶☷✅❌⭐⚡⚠✔✨❗❓")


_widths = {}


def text_width(text):
    if text.isascii():
        return len(text)
    w = _widths
    total = 0
    for c in text:
        n = w.get(c)
        if n is None:
            n = w[c] = cell_width(c)
        total += n
    return total


def fit(text, width, align="left"):
    """Cuts with … or pads with spaces to the exact width."""
    if width <= 0:
        return ""
    if text_width(text) > width:
        out, used = "", 0
        for c in text:
            if used + cell_width(c) > width - 1:
                break
            out, used = out + c, used + cell_width(c)
        return out + "…" + " " * (width - used - 1)
    pad = " " * (width - text_width(text))
    return pad + text if align == "right" else text + pad



def slice_ansi(text, a, b):
    """Slice of the line between columns a and b (counting from 0), keeping the colors:
    all the color codes pass, the letters only inside the slice."""
    out, col, i = [], 0, 0
    while i < len(text):
        m = ANSI.match(text, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        c = text[i]
        w = cell_width(c)
        if a <= col and col + w <= b:
            out.append(c)
        elif col < b and col + w > a:
            out.append(" " * (min(b, col + w) - max(a, col)))
        col += w
        i += 1
    if col < b and b != float("inf"):
        out.append(" " * (b - max(a, col)))
    return "".join(out)


def duration_short(seconds):
    """How Claude counts the turn time: 16s, 3m 55s, 1h 2m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def visible_width(text):
    return text_width(ANSI.sub("", text))


# Windows Terminal draws Sixel in a "virtual" cell of 10x20 pixels.
CELL_W, CELL_H = 10, 20
MODE_RE = re.compile(r"(⏵⏵|⏸)\s*([a-z][a-z ]*?)\s+on\b")   # "⏵⏵ auto mode on", "⏸ plan mode on"
MODE_NAMES = {"auto": "auto mode", "default": "manual mode", "plan": "plan mode", "acceptEdits": "accept edits on",
              "bypassPermissions": "bypass permissions"}
THUMB_ROWS = 7   # height of the pasted-image thumbnails (the last line is the number)
_sixels = {}   # (file, data, rows) → (sixel text, columns it takes)


def sixel(path, rows):
    """Thumbnail of the image in Sixel, `rows` lines high. Returns (text, columns)."""
    try:
        key = (path, os.path.getmtime(path), rows)
    except OSError:
        return "", 0
    if key in _sixels:
        return _sixels[key]
    try:
        from PIL import Image
        img = Image.open(path)
        img.thumbnail((CELL_W * 28, CELL_H * rows))
        img = img.convert("RGB").quantize(colors=64)
    except Exception:
        _sixels[key] = ("", 0)
        return _sixels[key]
    w, h = img.size
    pal = img.getpalette()[: 64 * 3]
    px = img.load()
    out = ["\x1bP0;1;0q", f'"1;1;{w};{h}']
    for i in range(len(pal) // 3):
        r, g, b = pal[3 * i: 3 * i + 3]
        out.append(f"#{i};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}")
    for top in range(0, h, 6):
        bands = {}
        for x in range(w):
            for dy in range(min(6, h - top)):
                c = px[x, top + dy]
                bands.setdefault(c, [0] * w)[x] |= 1 << dy
        for c, bits in bands.items():
            out.append(f"#{c}")
            run, prev, count = [], None, 0
            for v in bits + [None]:
                if v == prev:
                    count += 1
                    continue
                if prev is not None:
                    ch = chr(63 + prev)
                    run.append(f"!{count}{ch}" if count > 3 else ch * count)
                prev, count = v, 1
            out.append("".join(run) + "$")
        out.append("-")
    out.append("\x1b\\")
    _sixels[key] = ("".join(out), -(-w // CELL_W))
    return _sixels[key]


def fit_ansi(text, width):
    """Like fit, for colored text: pads with space; if it overflows, strips the color and cuts."""
    w = visible_width(text)
    if w <= width:
        return text + " " * (width - w)
    return fit(ANSI.sub("", text), width)


_wraps = {}


def wrap(text, width):
    key = (text, width)
    if key not in _wraps:
        if len(_wraps) > 6000:
            _wraps.clear()
        _wraps[key] = _wrap(text, width)
    return list(_wraps[key])


def _wrap(text, width):
    """Breaks into lines of up to `width` columns, respecting the text's line breaks."""
    out = []
    for para in str(text).splitlines() or [""]:
        line = ""
        for word in para.split(" "):
            while text_width(word) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            if not line:
                line = word
            elif text_width(line) + 1 + text_width(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


def clean_markdown(text):
    """Strips the most common Markdown marks to read in the terminal."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    # Tab and other control characters change the width in the terminal and push the screen.
    text = text.replace("\t", "    ").replace("\r", "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text.replace("```", "")


# Colors of the conversation text (dark theme in shades of blue).
def rgb(hex_color, back=False):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"{ESC}{48 if back else 38};2;{r};{g};{b}m"


G_TEXT = rgb("#D9DEE7")
G_HEAD = rgb("#8AB4F8")
G_STRONG = BOLD + rgb("#FFFFFF")
G_EMPH = ESC + "3m" + rgb("#B9C6DA")
G_CODE = rgb("#9CC3FF") + rgb("#1E2633", back=True)
G_BLOCK = rgb("#B9C6DA") + rgb("#1E2633", back=True)
G_LINK = ESC + "4m" + rgb("#8AB4F8")   # underline: it can be clicked
URL_BARE = re.compile(r"(?<![\x01\w/])(https?://[^\s<>`\x01\x02]*[^\s<>`\x01\x02.,;:!?)\]'\"])")
G_HR = rgb("#3A4A63")
CODE = G_CODE


def md_inline(line, state):
    """**bold**, *italic*, `code` and links in the theme colors;
    state = [bold, code, italic] passes from one line to the next."""
    line = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", "\x01\\1\x02", line)
    # Bare link (https://…) also highlighted, outside code spans.
    parts = line.split("`")
    for k in range(0, len(parts), 2):
        parts[k] = URL_BARE.sub("\x01\\1\x02", parts[k])
    line = "`".join(parts)
    while len(state) < 3:
        state.append(False)

    def now():
        return (RESET + G_TEXT + (G_STRONG if state[0] else "") + (G_EMPH if state[2] else "")
                + (G_CODE if state[1] else ""))

    out, i = [now()], 0
    while i < len(line):
        c = line[i]
        if line.startswith("**", i) and not state[1]:
            state[0] = not state[0]
            out.append(now())
            i += 2
            continue
        if c == "`":
            state[1] = not state[1]
            out.append(now() + " " if state[1] else " " + now())
            i += 1
            continue
        if c == "*" and not state[1] and (state[2] or (i + 1 < len(line) and line[i + 1] not in " *")):
            state[2] = not state[2]
            out.append(now())
            i += 1
            continue
        if c == "\x01":
            out.append(G_LINK)
        elif c == "\x02":
            out.append(now())
        else:
            out.append(c)
        i += 1
    return "".join(out) + RESET


_md = {}


def md_lines(text, width):
    key = (text, width)
    if key not in _md:
        if len(_md) > 4000:
            _md.clear()
        _md[key] = _md_lines(text, width)
    return list(_md[key])


def _md_lines(text, width):
    """Claude text as terminal lines: headings, lists, tables and code blocks
    as the session draws them, wrapping at the column size."""
    text = (text or "").replace("\t", "    ").replace("\r", "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    src = text.split("\n")
    out, i = [], 0
    while i < len(src):
        line = src[i]
        if line.strip().startswith("```"):
            i += 1
            while i < len(src) and not src[i].strip().startswith("```"):
                out.append(G_BLOCK + fit("  " + src[i], width) + RESET)
                i += 1
            i += 1
            continue
        if re.fullmatch(r"\s*([-*_])\s*(\1\s*){2,}", line):
            out.append(G_HR + "--------" + RESET)
            i += 1
            continue
        if line.strip().startswith("|"):
            table = []
            while i < len(src) and src[i].strip().startswith("|"):
                cells = [c.strip() for c in src[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    table.append(cells)
                i += 1
            ncol = max(len(r) for r in table) if table else 0
            plain = [[re.sub(r"\*\*|`", "", c) for c in r] + [""] * (ncol - len(r)) for r in table]
            widths = [max(visible_width(r[c]) for r in plain) for c in range(ncol)]
            # Does not fit: the short columns stay whole and the long ones share the rest. The
            # cell text wraps onto more lines, instead of being cut with "…".
            avail = width - 3 * (ncol - 1)
            if ncol and sum(widths) > avail:
                todo, left, fixed = set(range(ncol)), avail, list(widths)
                while todo:
                    share = max(4, left // len(todo))
                    small = {c for c in todo if widths[c] <= share}
                    if not small:
                        for c in todo:
                            fixed[c] = share
                        break
                    left -= sum(widths[c] for c in small)
                    todo -= small
                widths = fixed
            for ri, r in enumerate(plain):
                parts = [wrap(r[c], widths[c]) or [""] for c in range(ncol)]
                for k in range(max(len(p) for p in parts)):
                    cells = [(p[k] if k < len(p) else "") for p in parts]
                    cells = [s + " " * max(0, widths[c] - visible_width(s)) for c, s in enumerate(cells)]
                    row = (DIM + " │ " + RESET).join(cells)
                    out.append((BOLD if ri == 0 else "") + fit_ansi(row, width) + RESET)
                if ri == 0:
                    out.append(DIM + fit("─┼─".join("─" * w for w in widths), width).rstrip() + RESET)
            continue
        i += 1
        if not line.strip():
            out.append("")
            continue
        heading = re.match(r"^(#+)\s*(.*)", line)
        if heading:
            title = re.sub(r"\*\*|`", "", heading.group(2))
            if len(heading.group(1)) == 1:
                out.append(BOLD + rgb("#FFFFFF") + rgb("#1F3A5F", back=True) + fit(f" {title} ", width).rstrip() + RESET)
            else:
                out.append(BOLD + G_HEAD + fit(heading.group(1) + " " + title, width).rstrip() + RESET)
            continue
        bullet = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)", line)
        if bullet:
            indent, mark, body = bullet.groups()
            mark = "•" if mark in "-*" else mark
            lead = indent + mark + " "
            hang = " " * len(lead)
            state = [False, False, False]
            for n, part in enumerate(wrap(body, max(10, width - len(lead)))):
                out.append((lead if n == 0 else hang) + md_inline(part, state))
            continue
        state = [False, False, False]
        for part in wrap(line, width):
            out.append(md_inline(part, state))
    return out


def read_frontmatter(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(3000)
    except OSError:
        return {}
    m = re.match(r"---\s*\n(.*?)\n---", head, re.S)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        k, _, v = line.partition(":")
        if v:
            out[k.strip()] = v.strip().strip("'\"")
    return out


BUILTIN_COMMANDS = [
    ("compact", "compact the conversation and free context"),
    ("clear", "clear the conversation and start over"),
    ("model", "switch the model (opus, sonnet, haiku…)"),
    ("effort", "switch the effort (low, medium, high, xhigh, max)"),
    ("context", "show what is using the context"),
    ("cost", "cost and tokens of this session"),
    ("usage", "plan usage (5h and 7d)"),
    ("review", "review a pull request"),
    ("code-review", "review the current diff"),
    ("security-review", "security review of the changes"),
    ("simplify", "clean up and simplify the changed code"),
    ("init", "create the project's CLAUDE.md"),
    ("memory", "edit memory (CLAUDE.md)"),
    ("rename", "rename the session"),
    ("export", "export the conversation"),
    ("rewind", "rewind the conversation to an earlier point"),
    ("status", "version, model and account"),
    ("mcp", "MCP servers"),
    ("agents", "subagents"),
    ("hooks", "configured hooks"),
    ("permissions", "tool permissions"),
    ("config", "settings"),
    ("loop", "repeat a prompt on an interval"),
    ("schedule", "schedule a task"),
    ("todos", "the session's task list"),
    ("doctor", "check the installation"),
    ("help", "help and the list of commands"),
    ("exit", "close the session"),
    ("resume", "resume an earlier conversation"),
    ("add-dir", "add a folder the session can work in"),
    ("plugin", "manage plugins"),
    ("theme", "switch the color theme"),
    ("statusline", "set up the status line"),
    ("release-notes", "what changed in each version"),
    ("feedback", "send feedback about Claude Code"),
    ("keybindings", "keyboard shortcuts"),
    ("upgrade", "upgrade the plan"),
]

_init_cmds = {}   # (folder, plugins) → names the session itself says it accepts
_init_running = set()


def _load_init_cmds(key):
    """Asks Claude, the way the session runs (same folder and plugins), for the list of
    its commands: the -p init brings them all (skills, plugins, mods). Stops right after init."""
    cwd, plugins = key
    args = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
            "--model", "haiku"]
    for d in plugins:
        args += ["--plugin-dir", d]
    try:
        p = subprocess.Popen(args + ["x"], cwd=cwd or None, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                             creationflags=0x08000000)
        try:
            limit = time.time() + 30
            for line in p.stdout:
                if time.time() > limit:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") == "system" and entry.get("subtype") == "init":
                    names = set(entry.get("slash_commands") or []) | set(entry.get("terminal_slash_commands") or [])
                    _init_cmds[key] = (time.time(), {n for n in names if not n.startswith("__")})
                    break
        finally:
            p.kill()
    except Exception:
        pass
    finally:
        _init_running.discard(key)


def session_commands(row):
    """Names of the / commands of the session, read from Claude itself (in the background, kept 10 min)."""
    state = read_json(os.path.join(HOME, "jobs", row.get("id", ""), "state.json"), {}) if row.get("id") else {}
    flags = state.get("respawnFlags") if isinstance(state.get("respawnFlags"), list) else []
    plugins = tuple(flags[i + 1] for i, f in enumerate(flags[:-1]) if f == "--plugin-dir")
    key = (row.get("cwd") or "", plugins)
    hit = _init_cmds.get(key)
    if (not hit or time.time() - hit[0] > 600) and key not in _init_running:
        _init_running.add(key)
        threading.Thread(target=_load_init_cmds, args=(key,), daemon=True).start()
    return hit[1] if hit else set()


def slash_commands(cwd):
    """The / commands the session accepts: skills and commands of its project and of the user."""
    def load():
        found = {}
        for base in (os.path.join(cwd or "", ".claude"), os.path.join(HOME)):
            skills = os.path.join(base, "skills")
            for name in sorted(os.listdir(skills)) if os.path.isdir(skills) else []:
                meta = read_frontmatter(os.path.join(skills, name, "SKILL.md"))
                if not meta or meta.get("user-invocable") == "false":
                    continue
                found.setdefault(meta.get("name") or name, meta.get("description", ""))
            commands = os.path.join(base, "commands")
            for name in sorted(os.listdir(commands)) if os.path.isdir(commands) else []:
                if name.endswith(".md"):
                    meta = read_frontmatter(os.path.join(commands, name))
                    found.setdefault(name[:-3], meta.get("description", ""))
        # Installed plugins: their commands have the plugin name in front (plugin:skill).
        installed = read_json(os.path.join(HOME, "plugins", "installed_plugins.json"), {})
        for key, entries in ((installed or {}).get("plugins") or {}).items():
            plugin = key.split("@")[0]
            root = (entries[-1] or {}).get("installPath") if isinstance(entries, list) and entries else None
            if not root:
                continue
            for kind in ("skills", "commands"):
                folder = os.path.join(root, kind)
                for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
                    path = os.path.join(folder, name, "SKILL.md") if kind == "skills" else os.path.join(folder, name)
                    if kind == "commands" and not name.endswith(".md"):
                        continue
                    meta = read_frontmatter(path)
                    if kind == "skills" and (not meta or meta.get("user-invocable") == "false"):
                        continue
                    base = meta.get("name") or (name[:-3] if kind == "commands" else name)
                    found.setdefault(f"{plugin}:{base}", meta.get("description", ""))
        # Claude Code's own commands (they work in the background session).
        for name, desc in BUILTIN_COMMANDS:
            found.setdefault(name, desc)
        return sorted(found.items())
    return cached(("cmds", cwd), 30, load) or []


BANG = re.compile(r"```[^\n]*\n([\s\S]*?)```")


def last_bang(turns):
    """copiar-comando: the last "! " command after your last request, in a code
    block, in `backticks` or on a loose line (Claude writes it all three ways)."""
    for turn in reversed(turns):
        if turn[0] == "you":
            return None
        if turn[0] == "claude":
            text, found = turn[1], []   # (position in the message, command)
            fences = [(m.start(), m.end()) for m in BANG.finditer(text)]
            for m in BANG.finditer(text):
                body = m.group(1).strip()
                if body.startswith("! "):
                    found.append((m.start(), " ".join(l.strip() for l in body[2:].splitlines() if l.strip())))
            outside = lambda pos: not any(a <= pos < b for a, b in fences)
            for m in re.finditer(r"`!\s+([^`\n]+)`", text):
                if outside(m.start()):
                    found.append((m.start(), m.group(1).strip()))
            for at, cmd in bang_paragraphs(text):   # loose paragraph, of one or several lines
                found.append((at, cmd[2:].strip().rstrip("`")))
            if found:
                return max(found)[1]
    return None


CODE_BG = "48;2;30;38;51"   # the background of code spans (G_CODE and G_BLOCK)
COPY_MARK = "🗐"        # copy (two sheets): on Windows Terminal it takes just one column
COPY_BLOCK = COPY_MARK


def code_runs(line):
    """Spans with code background in an already colored line: [(start column, end, text)]."""
    runs, col, on, start, text = [], 0, False, 0, []
    i = 0
    while i < len(line):
        m = ANSI.match(line, i)
        if m:
            code = m.group()
            if CODE_BG in code:
                if not on:
                    on, start, text = True, col, []
            elif code in (RESET, ESC + "0m") and on:
                runs.append((start, col - 1, "".join(text)))
                on = False
            i = m.end()
            continue
        c = line[i]
        if on:
            text.append(c)
        col += cell_width(c)
        i += 1
    if on:
        runs.append((start, col - 1, "".join(text)))
    return runs


def _put(line, col, mark):
    """Replaces the cells from col onward with the mark (1 or 2 columns), keeping the colors."""
    return slice_ansi(line, 0, col) + mark + slice_ansi(line, col + text_width(mark), float("inf"))


def wrap_spans(text, width):
    """Breaks the text into lines of up to width columns, preferably after a space,
    losing nothing: returns [(start, end)] in the text (the lines, together, are the text)."""
    spans, start = [], 0
    while start < len(text):
        used, end = 0, start
        while end < len(text) and used + cell_width(text[end]) <= width:
            used += cell_width(text[end])
            end += 1
        if end < len(text):
            space = text.rfind(" ", start, end)
            if space > start:
                end = space + 1
        spans.append((start, end))
        start = end
    return spans or [(0, 0)]


def code_sources(text):
    """The code spans of the original message, in the order the screen draws them:
    whole ``` blocks and `inline` (outside tables and headings, which the screen shows uncolored)."""
    out, lines, i = [], (text or "").replace("\r", "").split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            out.append("\n".join(lines[i + 1:j]))
            i = j + 1
            continue
        if not line.strip().startswith(("|", "#")):
            out += re.findall(r"`([^`]+)`", line)
        i += 1
    return out


def add_copy_buttons(convo, buttons, start=0, sources=None):
    """Puts a 🗐 at the end of each blue span (inline code and code block) and stores
    its text in buttons, for the click to copy. The 🗐 takes the place of the space that closes
    the span (or of the last cell of the block): the line does not change width. With sources (the
    original text), copies the whole span, even what the screen cut or wrapped."""
    found = []   # ([(line, start column, end)], screen text): where the click copies
    carry, i = "", start
    carry_at = []
    while i < len(convo):
        line = convo[i]
        block = lambda l: l[2:].startswith(G_BLOCK)   # the line comes with "● " or "  " in front
        if block(line):   # code block: one button for the whole block
            j = i
            while j < len(convo) and block(convo[j]):
                j += 1
            text = "\n".join(ANSI.sub("", l)[4:].rstrip() for l in convo[i:j]).strip("\n")
            width = visible_width(line)
            if width > 5:
                convo[i] = _put(line, width - 3, COPY_BLOCK)
                found.append(([(k, 2, width - 1) for k in range(i, j)], text))   # the whole block is clickable
            i = j
            continue
        runs = code_runs(line)
        for n, (a, b, text) in enumerate(runs):
            closed = text.endswith(" ") or n < len(runs) - 1   # the trailing space closes the span
            at = [(i, a, b)]
            if n == 0 and carry and a <= 4:
                text = carry + " " + text.strip()   # the span started on the line above
                at = carry_at + at
            carry, carry_at = "", []
            if closed:
                line = _put(line, b, COPY_MARK)
                found.append((at, text.strip()))
            else:
                carry, carry_at = text.strip(), at   # broke at the end of the line: continues on the next
        convo[i] = line
        i += 1
    # Same number of spans on screen and in the original: copy the original (whole).
    exact = sources if sources is not None and len(sources) == len(found) else None
    for k, (spots, text) in enumerate(found):
        body = exact[k] if exact else text
        if body.strip():
            for line_no, a, b in spots:   # the whole blue span copies, not just the icon
                buttons.setdefault(line_no, []).append((a, b, ("copy", body)))


def bang_paragraphs(text):
    """Commands "! …" written as a loose paragraph (outside a code block): from "! " to
    the blank line. A multi-line command (ssh with a python script inside) comes
    whole, with the breaks: copying only the 1st line left the command half done.
    Returns [(position in the message, command)]."""
    out, fence, cur, pos = [], False, None, 0
    for raw in (text or "").replace("\r", "").split("\n"):
        here, pos = pos, pos + len(raw) + 1
        if raw.strip().startswith("```"):
            fence = not fence
            if cur:
                out.append(cur)
            cur = None
            continue
        if fence:
            continue
        if cur is not None:
            if raw.strip():
                cur = (cur[0], cur[1] + "\n" + raw.rstrip())
                continue
            out.append(cur)
            cur = None
        if raw.lstrip().startswith("! ") and len(raw.strip()) > 2:
            cur = (here, raw.strip())
    if cur:
        out.append(cur)
    return out


def add_link_buttons(convo, buttons, start, text):
    """Click on a link in the message opens it in the browser. [text](url) shows only the text: a
    click on it opens the url. Bare link broken over two lines: the 1st part opens the whole link."""
    links = [(m.group(1), m.group(2)) for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)]+)\)", text or "")]
    # Link inside `code` or a ``` block keeps the 🗐 (copy), does not open.
    loose = re.sub(r"```[\s\S]*?```|`[^`\n]*`|\[[^\]]+\]\([^)]+\)", "", text or "")
    bare = [m.group(1) for m in URL_BARE.finditer(loose)]
    if not links and not bare:
        return
    for i in range(start, len(convo)):
        plain = ANSI.sub("", convo[i])
        for label, url in links:
            for m in re.finditer(re.escape(label), plain):
                a = text_width(plain[:m.start()])
                buttons.setdefault(i, []).append((a, a + text_width(label) - 1, url))
        for m in re.finditer(r"https?://\S+", plain):
            shown = m.group().rstrip(".,;:)'\"" + COPY_MARK)
            url = next((u for u in bare if u.startswith(shown)), None)
            if url:
                a = text_width(plain[:m.start()])
                buttons.setdefault(i, []).append((a, a + text_width(shown) - 1, url))


def add_bang_buttons(convo, buttons, start, text, inner):
    """A "! …" command that Claude writes as a paragraph (no backticks): the screen shows it without
    a code background, so it gets no 🗐. Here it does: the whole paragraph copies the
    original command, on a single line, even when wrapped across several lines on screen."""
    commands = [cmd for _, cmd in bang_paragraphs(text)]
    i = start
    for cmd in commands:
        head = " ".join(cmd.split())[:20]
        while i < len(convo) and not " ".join(ANSI.sub("", convo[i])[2:].split()).startswith(head):
            i += 1
        if i >= len(convo):
            return
        j = i + 1   # the paragraph runs until the blank line
        while j < len(convo) and ANSI.sub("", convo[j]).strip():
            j += 1
        last = j - 1
        width = visible_width(convo[last])
        if width + 2 <= inner + 2:
            convo[last] = convo[last] + " " + COPY_MARK
        else:
            convo[last] = _put(convo[last], width - text_width(COPY_MARK), COPY_MARK)
        for k in range(i, j):
            buttons.setdefault(k, []).append((2, visible_width(convo[k]) - 1, ("copy", cmd)))
        i = j


def copy_text(text):
    env = dict(os.environ, AGENTS_CLIP=text)
    return subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $env:AGENTS_CLIP"],
                          env=env, capture_output=True, timeout=10, creationflags=0x08000000).returncode == 0


def copy_file(path):
    """Puts the file itself on the clipboard (like ctrl+c in Explorer): ctrl+v in
    Teams, WhatsApp or e-mail attaches the file. The path goes through the environment variable."""
    # Windows' own "Copy" (right click → Copy): only it sets everything Teams and
    # WhatsApp read (Shell IDList Array, FileGroupDescriptorW…); the file list alone did not paste.
    # OleFlushClipboard keeps the copy valid after powershell exits.
    script = ("Add-Type -AssemblyName System.Windows.Forms;"
              "Add-Type -Namespace W -Name Ole -MemberDefinition "
              "'[DllImport(\"ole32.dll\")] public static extern int OleFlushClipboard();';"
              "$p = $env:AGENTS_CLIP;"
              "$i = (New-Object -ComObject Shell.Application).Namespace((Split-Path $p)).ParseName((Split-Path $p -Leaf));"
              "if (-not $i) { exit 2 };"
              "$i.InvokeVerb('copy');"
              "$t = (Get-Date).AddMilliseconds(1500);"
              "while ((Get-Date) -lt $t) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 };"
              "[void][W.Ole]::OleFlushClipboard();"
              "if (-not [System.Windows.Forms.Clipboard]::ContainsFileDropList()) { exit 3 }")
    env = dict(os.environ, AGENTS_CLIP=path)
    return subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                          env=env, capture_output=True, timeout=20, creationflags=0x08000000).returncode == 0


TOOL_KEYS = ("command", "file_path", "notebook_path", "pattern", "url", "query", "skill", "description",
             "path", "action", "prompt")


def tool_line(name, data, result, err, width):
    """tool-lines: the tool on a single line — tool_call: Name(subject) - note —, without the output."""
    data = data if isinstance(data, dict) else {}
    key = next((k for k in TOOL_KEYS if isinstance(data.get(k), str) and data[k]), None)
    subject = data[key] if key else (json.dumps(data, ensure_ascii=False) if data else "")
    subject = " ".join(subject.split())
    notes = []
    text = result or ""
    if "[Request interrupted" in text or "interrupted by user" in text:
        notes.append("interrupted")
    elif err:
        notes.append("error")
    if "running in background with ID" in text:
        notes.append("background")
    if "Shell cwd was reset" in text:
        notes.append("shell cwd was reset")
    meta = ", ".join(notes)
    room = min(35, max(8, width - len(f"tool_call: {name}()") - (len(meta) + 3 if meta else 0) - 2))
    if len(subject) > room:
        subject = subject[: room - 1] + "…"
    color = rgb("#E5534B") if (err or "interrupted" in notes) else YELLOW if result is None else GREEN
    meta_txt = ((rgb("#E5534B") if err else DIM) + f" - {meta}" + RESET) if meta else ""
    return f"{color}●{RESET} {DIM}tool_call: {RESET}{BOLD}{name}{RESET}({subject}){meta_txt}"


def age(updated):
    s = max(0, int(time.time() - updated / 1000))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def short_model(model):
    return model.replace("claude-", "")


def home_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.lower().startswith(home.lower()) else path


# ---------------------------------------------------------------- app

class App:
    def __init__(self):
        self.rows = []
        self.loaded = False
        self.lock = threading.Lock()
        self.version, self.model, self.effort = header_info()
        self.cursor = 0          # position in the item list (titles + rows)
        self.collapsed = set()   # collapsed groups
        self.prompt = ""         # text of the bottom box
        self.mode = "task"       # task | reply | find
        self.reply_to = None
        self.find = ""
        self.help = False
        self.compose_new = False   # Kanban: ctrl+n turns the text into a new session, not a reply
        self.help_button = (0, 0)
        # Focus: chosen session and what you have already seen of each session ({id: ms}).
        self.fid = None
        self.hover = None          # Kanban: (session id, card area) under the mouse
        # Kanban: popup of the chosen card, on by default (ctrl+o toggles it and it is saved).
        self.peek = read_json(CONFIG_FILE, {}).get("peek", True)
        self.popup_lock = None     # Kanban: popup locked to the session while you reply through it
        self.cmd_sel = 0           # command / chosen in the menu
        self.tools_open = False    # ctrl+e: shows the tools' output (the ▸ of tool-lines)
        self.copied = None         # last "! command" copied with ctrl+y
        self.pscroll = 0           # scroll of the reply in the popup / Focus (pgup/pgdn)
        # ctrl+u: index of your prompts (like the ticks of the status-line mod).
        self.sel = None            # conversation excerpt selected with the mouse: (session, start, end)
        self.sel_drag = False
        self.index_open = False
        self.index_sel = None      # number of the prompt chosen in the index (1 = the first)
        self.index_jump = None     # prompt the conversation scrolls to on the next draw
        self.turn_limit = (None, 300)   # (session, conversation items loaded); grows when jumping to an old prompt
        self.seen = read_json(CONFIG_FILE, {}).get("seen", {})
        # New names (ctrl+r) that have not reached the session yet: they show up at once and
        # leave here when Claude Code confirms the name.
        self.names = read_json(CONFIG_FILE, {}).get("names", {})
        self.outbox = {}   # id → your messages that have not yet appeared in the conversation
        # Queue (as in Codex): tab saves the message; it goes on its own when the session
        # finishes its turn, one at a time. Kept on disk: F5 does not lose the queue.
        saved = read_json(QUEUE_FILE, {})
        self.waitlist = {k: v for k, v in saved.items() if isinstance(v, list) and v} if isinstance(saved, dict) else {}
        self.dispatched = {}   # id → (send time, turn_start from when it was sent)
        # id → when it started waiting for you (blinks until you look); survives F5.
        self.alerts = read_json(CONFIG_FILE, {}).get("alerts", {})
        self.features = read_json(CONFIG_FILE, {}).get("features", {})   # {key: False} = off
        self.feat_open = False
        self.feat_button = (0, 0)
        self.feat_hits = []   # (row, start column, end column, key) of each key in the panel
        self.find_hits, self.hits_for, self.searching = {}, "", False   # search inside the conversations
        self.waiting_button = (0, 0, 0)   # row and columns of the "waiting for you" button
        self.rang = 0.0
        self.notice = ""
        self.notice_until = 0    # until when the notice survives keypresses
        self.confirm = None      # item waiting for the second ctrl+x
        self.tick = 0
        self.queue = []          # keys already read from a VT sequence
        self.button = (0, 0)     # columns of the Refresh button on row 1
        self.view_button = (0, 0)  # columns of the Kanban/List button on row 1
        self.reload = False
        self.input_mode = None
        # Kanban (Ctrl+S switches the view): chosen column and card, first visible column,
        # scroll of each column, clickable areas and the card being dragged.
        self.view = "split"   # only the List + conversation view (the others left the screen)
        self.kcol, self.krow, self.kstart = 0, 0, 0
        self.kscroll = {}
        self.hits = []
        self.drag = None
        self.gsel = 0            # panel chosen in the Panels view
        self.ghits = []
        self.bcol, self.brow = 0, 0   # task chosen in the Projects view
        self.bscroll = {}
        self.bhits = []
        self.bgoal = 0                # chosen goal (0 = all)
        self.bside = False            # focus on the goals column
        self.shits = []
        threading.Thread(target=self.poll, daemon=True).start()

    # ------------------------------------------------ data

    def poll(self):
        # Every 3 s, or the moment some session's state changes (checking the state.json dates is
        # cheap); after the first pass, preloads the conversations.
        warmed = False
        while True:
            self.refresh()
            _safe(self.dispatch_waitlist)
            if not warmed:
                warmed = True
                self.background(self.prewarm_all)
            seen = self.state_stamps()
            time.sleep(0.5)   # a session writing nonstop does not turn into endless reading
            end = time.time() + REFRESH_S
            while time.time() < end:
                time.sleep(0.25)
                if self.state_stamps() != seen:
                    break

    def save_waitlist(self):
        try:
            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in self.waitlist.items() if v}, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def enqueue(self, row, text):
        self.waitlist.setdefault(row["id"], []).append({"text": text, "t": time.time()})
        self.save_waitlist()
        n = len(self.waitlist[row["id"]])
        self.notice, self.notice_until = (f"queued for {row['name']} ({n}) · goes when it finishes · alt+↑ edits"
                                          , time.time() + 6)

    def queue_key(self, ch):
        """Keys with the queue open: ↑↓ picks, enter edits, shift+↑↓ (or alt+↑↓) changes the
        order, del/ctrl+x/backspace cancels, esc closes."""
        sid = self.qedit["id"]
        items = self.waitlist.get(sid) or []
        if not items:
            self.qedit = None
            return True
        sel = max(0, min(self.qedit["sel"], len(items) - 1))
        code = self.getch() if ch in ("\x00", "\xe0") and self.queue else None
        if code in ("H", "P"):
            sel = max(0, min(len(items) - 1, sel + (-1 if code == "H" else 1)))
        elif code in (SHIFT_ARROWS["H"], SHIFT_ARROWS["P"], ALT_ARROWS["H"], ALT_ARROWS["P"]):
            to = sel + (-1 if code in (SHIFT_ARROWS["H"], ALT_ARROWS["H"]) else 1)
            if 0 <= to < len(items):
                items[sel], items[to] = items[to], items[sel]
                sel = to
                self.save_waitlist()
        elif ch == "\x0b":   # ctrl+k: empties this session's queue
            self.waitlist.pop(sid, None)
            self.save_waitlist()
            self.notice, self.notice_until = f"queue cleared ({len(items)} messages)", time.time() + 5
            self.qedit = None
            return True
        elif ch in ("\x18", "\b", "\x7f") or code == "S":   # S = Delete
            items.pop(sel)
            self.save_waitlist()
            self.notice, self.notice_until = "queued message cancelled", time.time() + 4
            sel = min(sel, len(items) - 1)
            if not items:
                self.qedit = None
                return True
        elif ch == "s":   # steer: sends now, mid-work
            self.queue_action("steer", sid, sel)
            return True
        elif ch in ("\r", "e"):
            row = next((r for r in self.rows if r["id"] == sid), None)
            if row:
                self.qedit, self.qediting = None, (sid, items[sel])
                self.mode, self.reply_to, self.prompt = "reply", row, items[sel]["text"]
                return True
        elif ch == "\x1b":
            self.qedit = None
            return True
        self.qedit["sel"] = sel
        return True

    def queue_action(self, action, sid, idx):
        """A queue button: steer sends now (the session reads it mid-work, like Enter),
        edit moves the text to the box, cancel removes it from the queue."""
        items = self.waitlist.get(sid) or []
        row = next((r for r in self.rows if r["id"] == sid), None)
        if not (0 <= idx < len(items)) or row is None:
            return
        if action == "edit":
            self.qedit, self.qediting = None, (sid, items[idx])
            self.mode, self.reply_to, self.prompt = "reply", row, items[idx]["text"]
            return
        m = items.pop(idx)
        self.save_waitlist()
        if getattr(self, "qedit", None) and self.qedit["id"] == sid:
            self.qedit["sel"] = max(0, min(self.qedit["sel"], len(items) - 1))
            if not items:
                self.qedit = None
        if action == "steer":
            self.background(self.reply, row, m["text"])
            self.notice, self.notice_until = f"sent to {row['name']} now", time.time() + 4
        else:
            self.notice, self.notice_until = "queued message cancelled", time.time() + 4

    def save_queued_edit(self):
        """Editing a queued one: enter/tab saves the text in its place (does not send)."""
        editing, self.qediting = getattr(self, "qediting", None), None
        if not editing or not isinstance(self.reply_to, dict) or self.reply_to["id"] != editing[0]:
            return False
        if editing[1] in self.waitlist.get(editing[0], []):
            editing[1]["text"] = self.with_images(self.prompt.strip())
            self.save_waitlist()
            self.notice, self.notice_until = "queued message updated", time.time() + 4
            self.qedit = {"id": editing[0], "sel": self.waitlist[editing[0]].index(editing[1])}
            return True
        return False

    def dispatch_waitlist(self):
        """Sends the next queued message of each session that finished its turn. After sending,
        waits for the session to start (and finish) the new turn before the next one."""
        if not self.on("auto_queue"):   # off in the features panel: the queue waits for you
            return
        for sid, items in list(self.waitlist.items()):
            row = next((r for r in self.rows if r["id"] == sid), None)
            if not items or row is None:
                continue
            if (getattr(self, "qedit", None) or {}).get("id") == sid or (getattr(self, "qediting", None) or ("",))[0] == sid:
                continue   # you are editing its queue: wait
            if row["busy"] or row.get("questions") or row.get("permission"):
                continue
            if len(items) > QUEUE_AUTO_MAX and self.on("queue_pause"):
                # Queue too large to be on purpose: does not send on its own, waits for you to look.
                self.notice, self.notice_until = (f"queue for {row['name']} paused: {len(items)} messages · "
                                                  "alt+↑ to review · ctrl+k clears it"), time.time() + 6
                continue
            if any(m["state"] == "sending" for m in self.outbox.get(sid, [])):
                continue
            _safe(live_turns, row.get("transcript"), 1)   # updates its turn (incremental read)
            start = (_meta.get(row.get("transcript")) or {}).get("turn_start")
            sent_at, start_then = self.dispatched.get(sid, (0, None))
            if sent_at and start == start_then and time.time() - sent_at < 90:
                continue   # the previous one has not become a turn in the conversation yet
            item = items.pop(0)
            self.save_waitlist()
            self.dispatched[sid] = (time.time(), start)
            self.background(self.reply, row, item["text"])

    def state_stamps(self):
        stamps = []
        for r in self.rows:
            try:
                stamps.append(os.path.getmtime(os.path.join(HOME, "jobs", r["id"], "state.json")))
            except OSError:
                stamps.append(0)
        return stamps

    def prewarm_all(self):
        """Reads each session's conversation in the background, to switch between them instantly."""
        for _ in range(40):   # waits for the first draw to tell the conversation width
            if getattr(self, "pane_inner", None):
                break
            time.sleep(0.1)
        with self.lock:
            rows = list(self.rows)
            item = self.current()
        # First the one on screen, then those waiting for you and those working.
        here = item[1]["id"] if item and item[0] == "row" else None
        rows.sort(key=lambda r: (r["id"] != here, r["group"] != "needs", not r["busy"]))
        for r in rows:
            path = r.get("transcript")
            if path:
                turns = _safe(live_turns, path, 300) or []
                _safe(all_prompts, path)
                # At the widths of the conversation beside it: the last draw's and those of List +
                # conversation and Kanban (same calculation as draw).
                cols = max(80, shutil.get_terminal_size((160, 45))[0] - 2)
                widths = {getattr(self, "pane_inner", None), cols - 60 - 1 - 4,
                          cols - max(64, cols * 55 // 100) - 1 - 4} - {None}
                for inner in widths:
                    for t in turns:
                        if t[0] == "claude":
                            _safe(md_lines, t[1], inner - 2)
                        elif t[0] == "you":
                            _safe(wrap, prompt_text(t[1]), inner - 15)
                time.sleep(0.05)   # lets the screen breathe between one and the next

    def refresh(self):
        try:
            rows = snapshot()
        except Exception as exc:
            self.notice = f"could not list sessions: {exc}"
            return
        # The list reorders itself (a session that changes state moves up or down): the
        # choice follows the session you are on, not the screen position.
        try:
            item = self.current() if self.loaded else None
        except Exception:
            item = None
        for r in rows:
            wanted = self.names.get(r["id"])
            if wanted and r["name"] == wanted:
                self.names.pop(r["id"], None)
                self.save_config()
            elif wanted:
                # The session brought back the old name (wrote the state over it): write it again.
                r["name"] = wanted
                self.rename_session(r, wanted)
        if self.loaded:
            # Finished or stopped at a question: blinks in the list and plays a sound.
            # Only counts if it stays stopped for 6 s: between one step and another the session
            # shows as stopped for a moment and the notice blinked for nothing.
            before = {r["id"]: r for r in self.rows}
            pending = getattr(self, "pending_alerts", {})
            fresh = []
            for r in rows:
                old = before.get(r["id"])
                if r["busy"]:
                    pending.pop(r["id"], None)
                    self.alerts.pop(r["id"], None)
                elif old and (old["busy"] or (r["group"] == "needs" and old["group"] != "needs")):
                    pending.setdefault(r["id"], time.time())
                if r["id"] in pending and not r["busy"] and time.time() - pending[r["id"]] >= 6:
                    pending.pop(r["id"])
                    self.alerts[r["id"]] = time.time()
                    fresh.append(r)
            self.pending_alerts = pending
            if fresh:
                self.ring()
                self.save_config()
        with self.lock:
            self.rows, self.loaded = rows, True
        if item and item[0] == "row":
            self.follow(item[1]["id"])
        elif item and item[0] == "head" and self.view not in ("kanban", "focus", "grid", "board"):
            self.cursor = next((i for i, it in enumerate(self.items()) if it[0] == "head" and it[1] == item[1]), self.cursor)
        if not self.seen and rows:
            # First time: what already exists counts as seen; the news counts from here on.
            self.seen = {r["id"]: int(time.time() * 1000) for r in rows}
            self.save_config()

    def pins(self):
        pins = read_json(PINS, [])
        return pins if isinstance(pins, list) else []

    def toggle_pin(self, row):
        pins = self.pins()
        if row["id"] in pins:
            pins.remove(row["id"])
        else:
            pins.append(row["id"])
        with open(PINS, "w", encoding="utf-8") as f:
            json.dump(pins, f, indent=2)

    def custom(self):
        """Groups created by you (Ctrl+G): {"order": [names], "sessions": {id: name}}.
        They live only in this replica: in the original the groups feature is off."""
        data = read_json(GROUPS_FILE, {})
        order = [n for n in data.get("order", []) if isinstance(n, str)]
        sessions = {k: v for k, v in (data.get("sessions") or {}).items() if isinstance(v, str)}
        return order, sessions

    def set_group(self, ids, name):
        order, sessions = self.custom()
        for sid in ids:
            if name:
                sessions[sid] = name
            else:
                sessions.pop(sid, None)
        if name and name not in order:
            order.append(name)
        self.save_custom(order, sessions)

    def rename_group(self, old, new):
        """Renames a group of yours, in the same place in the order and with the same sessions."""
        order, sessions = self.custom()
        if new in order and new != old:
            self.notice, self.notice_until = f"a group named {new} already exists", time.time() + 4
            return
        order = [new if n == old else n for n in order]
        sessions = {k: (new if v == old else v) for k, v in sessions.items()}
        self.save_custom(order, sessions)
        self.collapsed = {("custom:" + new) if c == "custom:" + old else c for c in self.collapsed}
        self.notice, self.notice_until = f"group {old} → {new}", time.time() + 4

    def rename_session(self, row, name):
        """Renames at once, like ctrl+r in claude agents: writes the name in the session's state
        (claude agents reads it from there) and in the conversation file (/resume reads it from there).
        Sends no message to the session either, even while it works."""
        state = os.path.join(HOME, "jobs", row["id"], "state.json")
        try:
            with _rename_lock:
                data = read_json(state, None)
                if not isinstance(data, dict):
                    raise OSError("session has no state")
                data["name"], data["nameSource"] = name, "user"
                with open(state + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(state + ".tmp", state)
            if row.get("transcript") and row.get("sessionId"):
                with open(row["transcript"], "a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "custom-title", "customTitle": name,
                                        "sessionId": row["sessionId"]}, ensure_ascii=False) + "\n")
            self.notice, self.notice_until = f"{name} ✓", time.time() + 4
        except OSError as exc:
            self.names.pop(row["id"], None)
            self.notice, self.notice_until = f"could not rename: {exc}", time.time() + 6

    def save_custom(self, order, sessions):
        order = [n for n in order if n in sessions.values()]
        with open(GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump({"order": order, "sessions": sessions}, f, indent=2, ensure_ascii=False)

    def group_of(self, item):
        """Name of the group of yours for the chosen item (group title or session inside it)."""
        if self.view == "kanban":
            columns = self.kanban_columns()
            key = columns[self.kcol][0] if 0 <= self.kcol < len(columns) else ""
        elif item and item[0] == "head":
            key = item[1]
        elif item and item[0] == "row":
            if item[1]["id"] in self.pins():
                return ""
            return self.custom()[1].get(item[1]["id"], "")
        else:
            return ""
        return key[len("custom:"):] if key.startswith("custom:") else ""

    def move_group(self, item, step):
        """Shift+↑ / Shift+↓: moves your group up or down (in Kanban, the column goes
        left or right). The choice stays on the same group."""
        name = self.group_of(item)
        if not name:
            self.notice = "shift+↑↓ only reorders groups you created (ctrl+g)"
            return
        order, sessions = self.custom()
        i = order.index(name)
        j = max(0, min(len(order) - 1, i + step))
        if i == j:
            return
        order.insert(j, order.pop(i))
        self.save_custom(order, sessions)
        key = "custom:" + name
        if self.view == "kanban":
            self.kcol = next((ci for ci, col in enumerate(self.kanban_columns()) if col[0] == key), self.kcol)
        elif item[0] == "head":
            self.cursor = next((i for i, it in enumerate(self.items()) if it[0] == "head" and it[1] == key), self.cursor)
        else:
            self.follow(item[1]["id"])
        self.notice = f"group {name} is now #{j + 1}"

    def start_moving(self, name):
        self.moving = name
        arrows = "← →" if self.view == "kanban" else "↑ ↓"
        self.notice, self.notice_until = f"moving group {name}: {arrows} move it · enter finishes", time.time() + 3600

    def move_named(self, name, step):
        """Moves the whole group one place up (or left, in Kanban)."""
        order, sessions = self.custom()
        if name not in order:
            self.moving = None
            return
        i = order.index(name)
        j = max(0, min(len(order) - 1, i + step))
        if i != j:
            order.insert(j, order.pop(i))
            self.save_custom(order, sessions)
        key = "custom:" + name
        if self.view == "kanban":
            self.kcol = next((ci for ci, col in enumerate(self.kanban_columns()) if col[0] == key), self.kcol)
        else:
            self.cursor = next((k for k, it in enumerate(self.items()) if it[0] == "head" and it[1] == key), self.cursor)
        arrows = "← →" if self.view == "kanban" else "↑ ↓"
        self.notice = f"moving group {name} (#{j + 1} of {len(order)}): {arrows} move it · enter finishes"

    def follow(self, row_id):
        if self.view == "focus":
            if any(r["id"] == row_id for r in self.focus_queue()):
                self.fid = row_id
            return
        if self.view == "grid":
            ids = [r["id"] for r in self.flat_rows()]
            if row_id in ids:
                self.gsel = ids.index(row_id)
            return
        if self.view == "kanban":
            for ci, (_, _, rows) in enumerate(self.kanban_columns()):
                for ri, r in enumerate(rows):
                    if r["id"] == row_id:
                        self.kcol, self.krow = ci, ri
            return
        moved = [i for i, it in enumerate(self.items()) if it[0] == "row" and it[1]["id"] == row_id]
        if moved:
            self.cursor = moved[0]

    # ------------------------------------------------ panels (Cursor's Agents Window style)

    def flat_rows(self):
        """Sessions in the same order as the list (pinned, your groups, states)."""
        return [r for _, rows in self.sections() for r in rows]

    def grid_selected(self):
        rows = self.flat_rows()
        if not rows:
            return None
        self.gsel = max(0, min(self.gsel, len(rows) - 1))
        return rows[self.gsel]

    def status_label(self, row):
        if row["busy"]:
            return GREEN + "● Running" + RESET, 9
        if row["group"] == "needs":
            return YELLOW + "● Needs input" + RESET, 13
        if row["alive"]:
            return "\x1b[38;2;90;150;235m● Completed" + RESET, 11
        return DIM + "● Stopped" + RESET, 9

    def tile(self, row, w, h, selected):
        """A panel of width w and height h (border included)."""
        edge = ORANGE if selected else DIM
        inner = w - 4
        status, status_len = self.status_label(row)
        name = fit(row["name"], max(4, inner - status_len - 3))
        head = f"{self.marker(row)} {BOLD}{name}{RESET} {status}"
        sub = f"{DIM}{fit(short_model(row['model']) + ' · ' + (row['effort'] or '—') + ' · ' + duration(row['elapsed']), inner)}{RESET}"
        summary = wrap(row["summary"] or "—", inner - 2)[:2]
        body = [head, sub, ""]
        body += [f"{DIM}│{RESET} " + fit(s, inner - 2) for s in summary]
        body.append("")
        tail_room = h - 2 - len(body) - 2
        text = [l for l in wrap(clean_markdown(row["last_text"]) or "(no reply yet)", inner) if l.strip()]
        body += [fit(l, inner) for l in text[-tail_room:]] if tail_room > 0 else []
        while len(body) < h - 4:
            body.append("")
        body.append(f"{DIM}{'─' * inner}{RESET}")
        message = f"Message {row['name']}… (space)"
        body.append(f"{DIM}{fit(message, inner)}{RESET}")
        top = f"{edge}╭{'─' * (w - 2)}╮{RESET}"
        bottom = f"{edge}╰{'─' * (w - 2)}╯{RESET}"
        return [top] + [f"{edge}│{RESET} {fit_ansi(l, inner)} {edge}│{RESET}" for l in body[:h - 2]] + [bottom]

    def grid_lines(self, cols, room, top_y):
        rows = self.flat_rows()
        selected = self.grid_selected()
        side_w = 30
        area_w = cols - side_w - 2   # "│ " separates the list from the panels
        tcols = 2 if area_w >= 90 else 1
        trows = 2 if room >= 30 else 1
        per_page = tcols * trows
        tile_w = (area_w - (tcols - 1)) // tcols
        tile_h = max(10, room // trows)
        page = self.gsel // per_page if rows else 0
        shown = rows[page * per_page:(page + 1) * per_page]
        self.ghits = []

        # Left list: 3 rows per session, scrolling to keep the chosen one.
        side = [f" {BOLD}Agents{RESET}{DIM}{len(rows):>{side_w - 8}}{RESET}", ""]
        per_side = max(1, (room - 2) // 3)
        sstart = max(0, min(self.gsel - per_side // 2, max(0, len(rows) - per_side)))
        for i in range(sstart, min(len(rows), sstart + per_side)):
            r = rows[i]
            status, _ = self.status_label(r)
            block = [f" {self.marker(r)} {fit(r['name'], side_w - 4)}",
                     f"   {DIM}{fit(short_model(r['model']), side_w - 4)}{RESET}",
                     f"   {status}"]
            y0 = top_y + len(side)
            if i == self.gsel:
                block = [BAR + l.replace(RESET, RESET + BAR) + " " * max(0, side_w - visible_width(l)) + RESET for l in block]
            side += block
            self.ghits.append((1, side_w, y0, y0 + 2, i))
        side += [""] * max(0, room - len(side))

        # Right panels.
        grid = []
        for tr in range(trows):
            tiles = []
            for tc in range(tcols):
                k = tr * tcols + tc
                if k < len(shown):
                    idx = page * per_page + k
                    tiles.append(self.tile(shown[k], tile_w, tile_h, idx == self.gsel))
                    x0 = side_w + 3 + tc * (tile_w + 1)
                    y0 = top_y + tr * tile_h
                    self.ghits.append((x0, x0 + tile_w - 1, y0, y0 + tile_h - 1, idx))
                else:
                    tiles.append([""] * tile_h)
            for i in range(tile_h):
                grid.append(" ".join(t[i] + " " * max(0, tile_w - visible_width(t[i])) for t in tiles))
        if len(rows) > per_page:
            pages = (len(rows) + per_page - 1) // per_page
            grid.append(f"{DIM}page {page + 1} of {pages} · ↑↓ to see the other sessions{RESET}")
        out = []
        for i in range(room):
            left = side[i] if i < len(side) else ""
            right = grid[i] if i < len(grid) else ""
            out.append(left + " " * max(0, side_w - visible_width(left)) + f"{DIM}│{RESET} " + right)
        return out

    # ------------------------------------------------ kanban

    def kanban_columns(self):
        """Kanban columns: the same sections as the list (Pinned, your groups, Needs
        input, Working, Completed), in the same order."""
        titles = dict(GROUPS, check=CHECK_TITLE)
        return [(key, key[len("custom:"):] if key.startswith("custom:") else titles.get(key, key), rows)
                for key, rows in self.sections()]

    def popup_target(self):
        """(session, card area) of the popup: the one you are replying to through it, the one
        under the mouse, or the one chosen with ctrl+o."""
        if self.mode == "reply" and self.reply_to and self.popup_lock and self.popup_lock[0] == self.reply_to["id"]:
            row = next((r for r in self.rows if r["id"] == self.popup_lock[0]), None)
            return (row, self.popup_lock[1]) if row else None
        if self.peek:
            row = self.kanban_selected()
            hit = next((h for h in self.hits if h[4] == self.kcol and h[5] == self.krow), None)
            if row:
                return (row, hit[:4] if hit else (0, 0, 0, 0))
        return None

    def session_pane(self, row, width, height):
        """Right side of Kanban: the chosen session's conversation (your requests and
        Claude's replies), the pending question and the reply box."""
        inner = width - 4
        self.pane_inner = inner
        if not row:
            body = [DIM + "pick a card with the arrow keys" + RESET]
            top, convo, foot, every, marks, buttons = body, [], [], [], [], {}
        else:
            chip, _ = self.chip(row)
            where = os.path.basename(row.get("cwd") or "")
            # Two lines: name, state and data on one; below, the summary of where the session is.
            turns = live_turns(row.get("transcript"), limit=300)
            extras = []
            local = localhost_of(row.get("cwd") or "") if self.on("localhost") else None
            if local:
                extras.append(GREEN + "▶ " + local + RESET)
            elif local == "":
                extras.append(DIM + "▶ localhost stopped" + RESET)
            arts = session_artifacts(turns) if self.on("artifacts") else []
            if arts:   # clickable: opens their list in place of the conversation
                extras.append(ESC + "48;2;45;55;72m" + f" ⧉ {len(arts)} artifact{'s' if len(arts) > 1 else ''} " + RESET)
            extras.append(ESC + "48;2;45;55;72m" + " ☰ index " + RESET)   # clickable, same as ctrl+u
            # No name and state: they are already in the list, beside. Starts with the mode.
            mode = self.mode_of(row) if self.on("mode") else ""
            head = "   ".join(p for p in (mode, "  ".join(extras)) if p)
            # Right after ☰ index: the folder the session is open in, the branch and what the
            # session would cost through the API. Without room, the path shortens from the start (…\GitHub\modsv2).
            cost = f"  💲 {cost_label(row)} API" if self.on("cost") and cost_label(row) else ""
            if row.get("cwd") and self.on("folder"):
                branch = f"  🌿 {row['branch']}" if row.get("branch") else ""
                parts = re.split(r"[\\/]", row["cwd"])
                # Without room, shrinks the path and then drops the branch: the cost always stays.
                for where in (f"📂 {row['cwd']}{branch}{cost}",
                              "📂 …\\" + "\\".join(parts[-2:]) + branch + cost,
                              "📂 " + parts[-1] + branch + cost, "📂 " + parts[-1] + cost,
                              cost.strip()):
                    if visible_width(head) + 3 + text_width(where) <= inner:
                        break
                head += "   " + DIM + where + RESET
            elif cost:
                head += "   " + DIM + cost.strip() + RESET
            # The state line (name, mode, localhost, ☰ index, folder, cost) goes at the foot of the
            # box, like the status line under the session; at the top only the summary stays.
            state_line = fit_ansi(head, inner)
            top = []
            brief = (session_brief(row, turns) or row.get("summary") or row.get("intent") or "") if self.on("summary") else ""
            brief = re.sub(r"(?i)(bearer\s+|token[=:]\s*)\S+", r"\1•••", brief)
            if brief:
                for n, l in enumerate(wrap(brief, inner - 8)[:2]):
                    top.append((ORANGE + BOLD + "SUMMARY " + RESET if n == 0 else " " * 8) + l)
                top.append(DIM + "─" * inner + RESET)
            # Live conversation: read from the file the session writes while it works
            # (Claude's text, the tools it uses and your requests).
            if self.pscroll == 0 and self.index_jump is None and _window.pop(row.get("transcript"), None) is not None:
                self.turn_limit = (None, 300)   # back at the end: the conversation goes back to the live one
            limit = self.turn_limit[1] if self.turn_limit[0] == row["id"] else 300
            turns = live_turns(row.get("transcript"), limit=limit)
            every = all_prompts(row.get("transcript"))
            number = len(every) - sum(1 for t in turns if t[0] == "you")   # prompts before the loaded excerpt
            # Up to this number, the prompt is from before the 8 MB the live conversation reads.
            marks = []   # (conversation line, prompt number)
            buttons = {}   # conversation line → [(start column, end, file)]
            convo = []
            for turn in turns:
                who = turn[0]
                if who == "you":
                    convo.append("")
                    number += 1
                    marks.append((len(convo), number))
                    # Date and time you sent it, at the right corner of the bubble's 1st line.
                    ms = stamp(turn[2], 0) if len(turn) > 2 and turn[2] and self.on("prompt_time") else 0
                    when = datetime.fromtimestamp(ms / 1000).strftime("%d/%m %H:%M") if ms else ""
                    room = inner - 2 - (len(when) + 2 if when else 0)
                    for n, l in enumerate(wrap(prompt_text(turn[1]), room)):
                        tail = (" " * max(0, room - visible_width(l)) + "  " + DIM + when + RESET + ESC + "48;2;55;55;55m"
                                if n == 0 and when else " " * max(0, inner - 2 - visible_width(l)))
                        convo.append(ESC + "48;2;55;55;55m" + ("> " if n == 0 else "  ") + l + tail + RESET)
                elif who == "tool":
                    _, name, label, result, data, err = turn
                    pending = result is None
                    if name == "AskUserQuestion" and pending and row.get("alive") and row.get("questions"):
                        # Open in the session: it shows in full below (NEEDS YOU), not here too.
                        convo += ["", YELLOW + "● " + RESET + BOLD + "Questions" + RESET + DIM + " · answer below ↓" + RESET]
                        continue
                    if name == "AskUserQuestion":
                        convo.append("")
                        dot = YELLOW if pending else GREEN
                        convo.append(dot + "● " + RESET + BOLD + "Questions" + RESET)
                        for qi, q in enumerate(data.get("questions") or [], 1):
                            if not isinstance(q, dict):
                                continue
                            for l in wrap(f"{qi}. {q.get('question', '')}", inner - 2):
                                convo.append("  " + (YELLOW + BOLD if pending else BOLD) + l + RESET)
                            for oi, o in enumerate(q.get("options") or [], 1):
                                o = o if isinstance(o, dict) else {"label": str(o)}
                                convo.append(f"    {YELLOW if pending else ''}{oi}) {o.get('label', '')}{RESET}")
                                if o.get("description"):
                                    convo += [DIM + "       " + l + RESET for l in wrap(o["description"], inner - 7)]
                        if not pending:
                            convo += [DIM + "  ⎿  " + l + RESET for l in wrap(" ".join(result.split()), inner - 5)[:4]]
                        convo.append("")
                        continue
                    # Consecutive tools stay together, with no blank line between them (tool-lines).
                    if not (convo and convo[-1].endswith("\x00")):
                        convo.append("")
                    convo.append(tool_line(name, data, result, err, inner) + "\x00")
                    if self.tools_open and not pending:
                        lines = result.replace("\t", "    ").replace("\r", "").split("\n")
                        while lines and not lines[-1].strip():
                            lines.pop()
                        for n, l in enumerate(lines[:12] or ["(no output)"]):
                            l = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", l)
                            convo.append(DIM + ("  ⎿  " if n == 0 else "     ") + fit(l, inner - 5).rstrip() + RESET)
                        if len(lines) > 12:
                            convo.append(DIM + f"     … +{len(lines) - 12} lines" + RESET)
                        convo[-1] += "\x00"
                else:
                    convo.append("")
                    body_lines = md_lines(turn[1], inner - 2)
                    first_line = len(convo)
                    for n, l in enumerate(body_lines):
                        convo.append(("● " if n == 0 else "  ") + l)
                    # 🗐 at the end of each blue stretch; copies the original, whole.
                    if self.on("code_copy"):
                        add_copy_buttons(convo, buttons, first_line, code_sources(turn[1]))
                    if self.on("bang_buttons"):
                        add_bang_buttons(convo, buttons, first_line, turn[1], inner)
                    if self.on("link_buttons"):
                        add_link_buttons(convo, buttons, first_line, turn[1])
                    # At the end of the message, a button for each file or image it mentions.
                    line, x = "  ", 2
                    files = message_files(turn[1], VIEW_EXT) if self.on("file_buttons") else []
                    if files:
                        convo.append("")   # breathing room between the text and the buttons
                    for path in files:
                        name = os.path.basename(path.rstrip("\\/")) or path
                        label = f" {'▣' if path.lower().endswith(IMAGE_EXT) else '↗'} {fit(name, 40).rstrip()} "
                        busy = getattr(self, "opening", None)
                        clicked = busy and busy[0] == path and time.time() - busy[1] < 4
                        if clicked:
                            label = f" ✓ opening {fit(name, 40).rstrip()}… "
                        w = text_width(label)
                        extra = text_width(" ⧉ copy file  ⌕ in folder ") if self.on("file_copy") and os.path.isfile(path) else 0
                        if x + w + extra > inner and x > 2:
                            convo += [line, ""]   # one free line between rows of buttons
                            line, x = "  ", 2
                        buttons.setdefault(len(convo), []).append((x, x + w - 1, path))
                        color = (ESC + "48;2;30;90;50m" + ESC + "38;2;200;255;210m") if clicked else (ESC + "48;2;45;55;72m" + ESC + "38;2;156;195;255m")
                        line += color + label + RESET
                        x += w
                        if self.on("file_copy") and os.path.isfile(path):
                            # Attached to the file button: copies the file itself, for ctrl+v in Teams.
                            copied = getattr(self, "file_copied", None)
                            done = copied and copied[0] == path and time.time() - copied[1] < 4
                            tag = " ✓ copied " if done else " ⧉ copy file "
                            buttons[len(convo)].append((x, x + text_width(tag) - 1, ("file", path)))
                            line += ESC + "48;2;35;42;56m" + ESC + ("38;2;200;255;210m" if done else "38;2;170;180;200m") + tag + RESET
                            x += text_width(tag)
                            # Plan B, works in any app: opens the folder with the file already selected; from there,
                            # drag into the conversation or ctrl+c / ctrl+v.
                            show = " ⌕ in folder "
                            buttons[len(convo)].append((x, x + text_width(show) - 1, ("show", path)))
                            line += ESC + "48;2;35;42;56m" + ESC + "38;2;170;180;200m" + show + RESET
                            x += text_width(show)
                        line += "  "
                        x += 2
                    if x > 2:
                        convo.append(line)
            convo = [l.replace("\x00", "") for l in convo]
            # Your messages not yet saved by the session: they show at once, with the send state.
            # "!ssh x" that you type comes back saved as "! ssh x": compares without the ! and spaces.
            same = lambda text: " ".join(prompt_text(text).split()).lstrip("! ")
            said = {same(t[1]) for t in turns[-40:] if t[0] == "you"}
            got = lambda text: same(text) in said or (len(same(text)) >= 60 and any(
                s.startswith(same(text)[:60]) for s in said))
            # Disappears once it appears in the conversation. One that did not stays a few minutes
            # with the reason and then leaves: stuck at the end, it looked like the conversation had stopped updating.
            box = [m for m in self.outbox.get(row["id"], [])
                   if (m["state"] == "closed" or not got(m["text"]))
                   and time.time() - m["t"] < (180 if m["state"] == "error" else 1800)]
            self.outbox.setdefault(row["id"], [])[:] = box   # draw already holds the lock
            # A "! command" only enters the conversation when it finishes: until then, with the session busy, it
            # is running, and what you sent afterwards waits in the session's queue.
            ahead = False
            for m in box:
                arrived = got(m["text"])
                if not arrived:   # the /exit already in the conversation keeps only the notice below
                    convo.append("")
                for n, l in enumerate([] if arrived else wrap(prompt_text(m["text"]), inner - 2)):
                    convo.append(ESC + "48;2;55;55;55m" + ("> " if n == 0 else "  ") + l
                                 + " " * max(0, inner - 2 - visible_width(l)) + RESET)
                state = {"sending": DIM + "  sending…", "ok": GREEN + "  ✓ sent",
                         "unsure": YELLOW + "  ? typed, not confirmed", "error": RED + "  ✗ not sent",
                         "closed": DIM + "  • session closed · your next message reopens it"}[m["state"]]
                if m["state"] != "ok" and m.get("why"):   # the reason stays with it, not only in the notice that disappears
                    state += DIM + " · " + fit(m["why"], max(0, inner - 20))
                if m["state"] == "ok" and not arrived and row.get("busy"):
                    mins = int((time.time() - m["t"]) // 60)
                    if ahead:
                        state = DIM + "  • queued in the session: goes when the command above finishes"
                    elif m["text"].lstrip().startswith("!"):
                        state = YELLOW + f"  {SPINNER[self.tick % len(SPINNER)]} running in the session · {mins}m" + DIM + " · the output shows up when it finishes"
                        ahead = True
                convo.append(state + RESET)
            bang = last_bang(turns)
            meta_now = _meta.get(row.get("transcript")) or {}
            compacting = self.compact_line(meta_now, inner) if self.on("compact_bar") else ""
            # While compacting, the bar takes the place of "✻ Working…" (the turn is the /compact itself).
            status = compacting or (self.turn_line(row, meta_now) if self.on("turn_line") else "")
            if status:
                convo += ["", status]
                # Each subagent and command running in the background, with the name and how long ago.
                if row.get("background") and self.on("background"):
                    convo += self.background_lines(row, turns, inner)
            foot = []
            dialog = self.dialog_of(row)
            if dialog and getattr(self, "panel", None) and self.panel["id"] == row["id"]:
                # Menu of a / command (/model, /config…): the keys go to it.
                foot.append(ORANGE + BOLD + "MENU · ↑↓ ←→ enter, letters and numbers go to the session · esc closes" + RESET)
                foot += ["  " + fit(l, inner - 2) for l in dialog[-16:]]
            elif dialog:
                # The question/permission as it is in the session: the number you press goes straight to it.
                asks = row.get("questions") or []
                # The session shows one question at a time; the tabs ("☒ Color ☐ Card") say which one is current.
                tabs = next((l for l in dialog if l.lstrip().startswith("←")), "")
                now = min(len(asks), tabs.count("☒") + 1) if asks else 0
                count = f" · question {now} of {len(asks)}" if len(asks) > 1 else ""
                multi = any(re.match(r"\s*(?:❯\s*)?\d+\.\s*\[.\]", l) for l in dialog)
                foot.append(YELLOW + BOLD + "NEEDS YOU" + count + RESET + DIM + (
                    " · pick several: type the numbers (e.g. 1 3) and enter · or type your own answer" if multi
                    else " · press the option number · or type your own answer and press enter" if asks
                    else " · press the option number right here") + RESET)
                foot += [x for x in (dialog_line(l, inner - 2) for l in dialog[-28:]) if x]
                for k, q in enumerate(asks[now:] if self.on("next_questions") else [], now + 1):   # the ones that come after this one
                    foot.append(DIM + fit(f"  next {k}/{len(asks)}: {q.get('question', '')}", inner) + RESET)
            elif (row.get("questions") or row.get("permission")) and not row["busy"]:
                wait = "opening the question…" if row["alive"] else "reply with the option number for each question (e.g. 1 2) or type"
                foot.append(YELLOW + BOLD + "NEEDS YOU " + RESET + YELLOW + fit(wait, inner - 16) + RESET)
            typing = self.prompt if self.mode == "reply" and self.reply_to and self.reply_to["id"] == row["id"] else ""
            menu = self.cmd_menu() if typing else []
            if menu:
                sel = self.cmd_sel % len(menu)
                first = max(0, min(sel - 3, len(menu) - 7))
                name_w = min(28, max(len(n) for n, _ in menu) + 2)
                for i in range(first, min(len(menu), first + 7)):
                    n, d = menu[i]
                    cell = fit("/" + n, name_w) + DIM + fit(d, inner - name_w - 1) + RESET
                    foot.append((ESC + "48;2;45;55;72m" + BOLD + "▸" + cell + RESET) if i == sel else " " + cell)
                foot.append(DIM + "↑↓ pick · tab or enter completes" + RESET)
            if bang and self.on("ctrl_y"):
                # copiar-comando: the last "! command" Claude sent, ready to paste.
                done = getattr(self, "copied", None) == bang
                foot.append((GREEN + "Copied ✓" if done else ORANGE + BOLD + "ctrl+y copies") + RESET
                            + DIM + " ! " + fit(bang, inner - 18).rstrip() + RESET)
            # The input box is only at the bottom of the screen (the same as the list's): not repeated here.
            if foot:
                foot.insert(0, "")   # free line between the conversation and the footer (ctrl+y, question…)
            ctx = context_lines(row, inner) if self.on("ctx_bar") else []   # the /context bar, above the state line
            foot += [DIM + "─" * inner + RESET] + ctx + [state_line]
        area = max(1, height - 2 - len(top) - len(foot))
        while convo and not convo[-1]:
            convo.pop()
        self.pane_area = area
        self.index_total = len(every)
        if self.index_jump is not None:
            # Enter in the index: the chosen prompt goes to the top of the conversation.
            hit = next((line for line, n in marks if n == self.index_jump), None)
            if hit is not None:
                self.pscroll = len(convo) - (hit + area)
            self.index_jump = None
        self.pscroll = max(0, min(self.pscroll, max(0, len(convo) - area)))
        end = len(convo) - self.pscroll
        first = max(0, end - area)
        shown = convo[first:end]
        shown += [""] * (area - len(shown))
        sel = self.sel if (row and self.sel and self.sel[0] == row["id"] and not self.index_open) else None
        if sel:
            (a_i, a_c), (b_i, b_c) = sorted(sel[1:])
            for k in range(len(shown)):
                i = first + k
                if a_i <= i <= b_i and i < len(convo):
                    plain = ANSI.sub("", convo[i])
                    c0 = a_c if i == a_i else 0
                    c1 = b_c + 1 if i == b_i else len(plain)
                    shown[k] = plain[:c0] + ESC + "7m" + plain[c0:c1] + RESET + plain[c1:]
        index_rows = {}
        art_rows = {}   # row of the artifacts list → link
        found = self.hits_of(row)
        if row and getattr(self, "arts_open", None) == row["id"]:
            # Click on ⧉: the session's artifacts, one per line; a click opens it in the browser.
            arts = artifact_titles(live_turns(row.get("transcript"), limit=300))
            shown = [ORANGE + BOLD + f"ARTIFACTS · {len(arts)}" + RESET + DIM + " · click one to open · esc closes" + RESET, ""]
            for name, url in arts[:max(0, area - 2)]:
                art_rows[len(shown)] = url
                shown.append("  ⧉ " + fit(name, max(10, inner - 4 - 40)) + DIM + "  " + fit(url, 40).rstrip() + RESET)
            shown += [""] * (area - len(shown))
        elif found and not self.index_jump:
            shown, index_rows = self.hits_list(found, inner, area)
        elif self.index_open and every:
            shown, index_rows = self.prompt_index(every, marks, first, end, inner, area)
        body = top + shown + foot
        # Scrollbar on the conversation's right edge: the light part is what is on screen,
        # and each ┤ is a prompt of yours (the same index as the status-line mod ticks).
        total = len(convo)
        bar = {}
        if total > area:
            thumb = max(1, area * area // total)
            pos = min(area - thumb, first * area // total)
            for i in range(area):
                bar[len(top) + i] = (ORANGE + "┃" + RESET) if pos <= i < pos + thumb else (DIM + "│" + RESET)
            for line, _ in (marks if self.on("scroll_ticks") else []):
                i = min(area - 1, line * area // total)
                bar[len(top) + i] = (ORANGE + BOLD + "┫" + RESET) if pos <= i < pos + thumb else (BOLD + "┤" + RESET)
        edge = ORANGE
        box = [f"{edge}╭{'─' * (width - 2)}╮{RESET}"]
        for i, line in enumerate(body[: max(1, height - 2)]):
            right = bar.get(i, edge + "│" + RESET)
            box.append(f"{edge}│{RESET} {fit_ansi(line, inner)} {right}")
        box.append(f"{edge}╰{'─' * (width - 2)}╯{RESET}")
        # Where the click lands (screen column and row, counting from 1): the ☰ index button,
        # each index row and each ┤ of the bar lead to the prompt.
        x0, y0 = getattr(self, "pane_at", (0, 0))
        hits = []
        if row and foot:
            # The ☰ index is on the last row of the box (the state one).
            last = min(len(body), max(1, height - 2)) - 1
            plain = ANSI.sub("", fit_ansi(body[last], inner))
            at = plain.find("☰ index")
            if at >= 0:
                x = x0 + 2 + text_width(plain[:at])
                hits.append((x - 1, x + 9, y0 + 1 + last, "index", None))
            at = plain.find("⧉ ")
            if at >= 0:
                x = x0 + 2 + text_width(plain[:at])
                w = text_width(plain[at:].split("  ")[0])
                hits.append((x - 1, x + w, y0 + 1 + last, "arts", None))
            at = plain.find("📂 ")
            if at >= 0 and row.get("cwd"):   # click on the folder copies its full path
                x = x0 + 2 + text_width(plain[:at])
                w = text_width(plain[at:].split("  ")[0])
                # 2 columns of slack: the terminal may draw a wider icon than the count.
                hits.append((x - 2, x + w + 2, y0 + 1 + last, "copy", row["cwd"]))
        for i, n in index_rows.items():
            hits.append((x0 + 2, x0 + 1 + inner, y0 + 1 + len(top) + i, "go", n))
        for i, url in art_rows.items():
            hits.append((x0 + 2, x0 + 1 + inner, y0 + 1 + len(top) + i, "open", url))
        if not index_rows and not art_rows:
            for i in range(first, end):
                for a, b, path in buttons.get(i, []):
                    # ("copy", text): the 🗐 of the blue stretches · ("file", path): copy the file
                    kind = ({"file": "copyfile", "show": "showfile"}.get(path[0], "copy")
                            if isinstance(path, tuple) else "open")
                    hits.append((x0 + 2 + a, x0 + 2 + b, y0 + 1 + len(top) + i - first,
                                 kind, path[1] if isinstance(path, tuple) else path))
        ticks = []   # the ┤ of the bar: the mouse over it shows the prompt
        if total > area:
            for line, n in (marks if self.on("scroll_ticks") else []):
                i = min(area - 1, line * area // total)
                hits.append((x0 + width - 2, x0 + width, y0 + 1 + len(top) + i, "go", n))
                ticks.append((x0 + width - 2, x0 + width, y0 + 1 + len(top) + i, n))
        self.pane_hits = (row, hits)
        self.tick_hits = (row, ticks)
        # Where the conversation text sits on screen, to select by dragging the mouse.
        self.pane_geo = (row["id"], x0, y0 + 1 + len(top), first, end, inner, convo) if row and not index_rows else None
        return box

    def select_mouse(self, kind, x, y):
        """Select conversation text: press, drag and release. On release, the stretch goes
        to the clipboard (ctrl+v pastes). Dragging past the top or bottom scrolls.
        Returns None when the mouse is not for the selection."""
        geo = getattr(self, "pane_geo", None)
        if kind == "down":
            self.sel_drag = False
            if not geo:
                return None
            rid, x0, top, first, end, inner, _ = geo
            hits = getattr(self, "pane_hits", (None, []))[1]
            if not (x0 + 2 <= x <= x0 + 1 + inner and top <= y < top + (end - first)) or any(
                    h[0] <= x <= h[1] and h[2] == y for h in hits):
                self.sel = None   # a click outside (or on a button) clears the selection
                return None
            pos = (first + y - top, x - x0 - 2)
            self.sel, self.sel_drag = (rid, pos, pos), True
            return True
        if not getattr(self, "sel_drag", False) or not geo or not self.sel:
            return None
        rid, x0, top, first, end, inner, convo = geo
        if kind == "move":
            if y < top:
                self.pscroll += 1
            elif y >= top + (end - first):
                self.pscroll = max(0, self.pscroll - 1)
            line = first + min(max(y, top), top + (end - first) - 1) - top
            self.sel = (self.sel[0], self.sel[1], (line, max(0, min(x - x0 - 2, inner - 1))))
            return True
        # Released: copies the stretch (if dragged; a single click copies nothing).
        self.sel_drag = False
        (a_i, a_c), (b_i, b_c) = sorted(self.sel[1:])
        if (a_i, a_c) == (b_i, b_c):
            self.sel = None
            return True
        lines = []
        for i in range(a_i, min(b_i, len(convo) - 1) + 1):
            plain = ANSI.sub("", convo[i])
            c0 = a_c if i == a_i else 0
            c1 = b_c + 1 if i == b_i else len(plain)
            piece = plain[c0:c1]
            if c0 == 0 and piece[:2] in ("● ", "  ", "> "):   # removes the conversation indent
                piece = piece[2:]
            # The 🗐 of code stretches belongs to the screen, not the text: it does not go in the copy.
            piece = piece.replace(COPY_MARK, "")
            lines.append(piece.rstrip())
        text = "\n".join(lines).strip("\n")
        n = text.count("\n") + 1
        self.notice, self.notice_until = f"copying {n} line{'s' if n > 1 else ''}…", time.time() + 3

        def run():
            ok = copy_text(text)
            self.notice = (f"copied ✓ {n} line{'s' if n > 1 else ''} · paste with ctrl+v" if ok
                           else "could not copy")
            self.notice_until = time.time() + 4
        threading.Thread(target=run, daemon=True).start()
        return True

    def ask_stop(self, row):
        """Esc on a working session: the first warns, the second (within 4 s) stops the
        work with `claude stop`. The conversation is kept: the next message continues it."""
        asked = getattr(self, "stopping", (None, 0))
        if asked[0] == row["id"] and time.time() - asked[1] < 4:
            self.stopping = (None, 0)
            self.notice, self.notice_until = f"stopping {row['name']}…", time.time() + 4

            def run():
                out = subprocess.run(["claude", "stop", row["id"]], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", creationflags=0x08000000)
                self.notice = (f"{row['name']} stopped · your next message continues the conversation" if out.returncode == 0
                               else f"could not stop {row['name']}: {(out.stderr or out.stdout).strip()[:80]}")
                self.notice_until = time.time() + 6
                self.refresh_soon = True
            threading.Thread(target=run, daemon=True).start()
            return True
        self.stopping = (row["id"], time.time())
        self.notice, self.notice_until = f"⚠ stop what {row['name']} is doing? esc again stops · any other key keeps it", time.time() + 4
        return True

    def ask_quit(self):
        """Esc (or ctrl+c) with nothing to cancel: asks before closing. A second
        esc within 4 seconds closes; any other key stays."""
        if time.time() - getattr(self, "quitting", 0) < 4:
            return False
        self.quitting = time.time()
        self.notice, self.notice_until = "⚠ quit the program? esc again quits · any other key stays", time.time() + 4
        return True

    def open_file(self, path):
        """File button: the button turns green ("opening…") at once and Windows opens the
        file in the background, without freezing the screen. Repeated clicks on the same file
        while it opens do not open it again."""
        name = os.path.basename(path.rstrip("\\/")) or path
        busy = getattr(self, "opening", None)
        if busy and busy[0] == path and time.time() - busy[1] < 4:
            self.notice, self.notice_until = f"already opening {name}…", time.time() + 3
            return
        self.opening = (path, time.time())
        self.notice, self.notice_until = f"opening {name}…", time.time() + 4

        def run():
            try:
                # .md opens formatted in the browser, not as raw text in the editor.
                md = path.lower().endswith((".md", ".markdown")) and os.path.isfile(path) and self.on("md_browser")
                open_detached(md_page(path) if md else path)
            except OSError:
                self.notice, self.notice_until = f"could not open {path}", time.time() + 5
                self.opening = None
        threading.Thread(target=run, daemon=True).start()

    def prompt_index(self, every, marks, first, end, inner, area):
        """ctrl+u: all your prompts of the session, numbered, in place of the conversation.
        The ones on screen now are orange; enter scrolls the conversation to the chosen one."""
        total = len(every)
        self.index_sel = max(1, min(total, self.index_sel or total))
        on_screen = {n for line, n in marks if first <= line < end}
        lines = [ORANGE + BOLD + f"INDEX · {total} prompt{'s' if total > 1 else ''}" + RESET
                 + DIM + "  ↑↓ pick · enter jumps there · esc closes" + RESET, ""]
        rows = area - len(lines)
        start = max(0, min(self.index_sel - 1 - rows // 2, total - rows))
        num_w = len(str(total))
        where = {}   # list row → prompt number (for the click)
        for n in range(start + 1, min(total, start + rows) + 1):
            where[len(lines)] = n
            text = " ".join(prompt_text(every[n - 1]).split())
            cell = f"{n:>{num_w}}. " + fit(text, inner - num_w - 4)
            if n == self.index_sel:
                lines.append(ESC + "48;2;45;55;72m" + BOLD + "▸ " + cell + " " * max(0, inner - 2 - visible_width(cell)) + RESET)
            else:
                lines.append("  " + (ORANGE if n in on_screen else "") + cell + RESET)
        return lines + [""] * (area - len(lines)), where

    def index_go(self, row):
        """Enter in the index: loads the conversation from the chosen prompt, if it is old,
        and asks the next draw to scroll to it."""
        path = row.get("transcript")
        total = len(all_prompts(path))
        if not self.index_sel or self.index_sel > total:
            return
        full = live_turns(path, limit=10 ** 9)
        spots = [i for i, t in enumerate(full) if t[0] == "you"]
        floor = total - len(spots)
        if self.index_sel <= floor:
            # Prompt from before the excerpt read: starts reading the conversation from it.
            offset = prompt_offset(path, self.index_sel)
            if offset is None:
                return
            _window[path] = max(0, offset - 1)
            full = live_turns(path, limit=10 ** 9)
            spots = [i for i, t in enumerate(full) if t[0] == "you"]
            floor = total - len(spots)
            if self.index_sel <= floor:
                self.notice = f"could not open prompt {self.index_sel}"
                return
        need = len(full) - spots[self.index_sel - floor - 1]
        if need > 300:
            self.turn_limit = (row["id"], need + 20)
        self.index_jump = self.index_sel
        self.index_open = False
        self.notice = f"prompt {self.index_sel} of {total}"

    def reply_target(self, fallback):
        """With the popup open, what you type goes to its session (and it stays
        locked to it until you send); without a popup, it goes to the chosen card."""
        target = self.popup_target() if self.view == "kanban" else None
        if target:
            self.popup_lock = (target[0]["id"], target[1])
            return target[0]
        self.popup_lock = None
        return fallback

    def kanban_popup(self, lines, cols):
        """Popup of "where we stopped" beside the card under the mouse (or the chosen one, with ctrl+o)."""
        target = self.popup_target()
        if not target:
            return lines
        row, (x0, x1, y0, _) = target
        pw = min(76, cols - 4)
        ph = min(len(lines) - 6, 24)
        typing = self.prompt if self.mode == "reply" and self.reply_to and self.reply_to["id"] == row["id"] else None
        box = self.focus_box(row, pw, ph, edge=ORANGE, compact=True, typing=typing if typing is not None else "")
        bw = pw + 1   # focus_box starts with a space
        if x1 + 2 + bw <= cols:
            left = x1 + 1
        elif x0 - 2 - bw >= 0:
            left = x0 - 2 - bw
        else:
            left = max(0, (cols - bw) // 2)
        top = max(4, min(y0 - 1, len(lines) - len(box)))
        out = list(lines)
        for i, part in enumerate(box):
            y = top + i
            if 0 <= y < len(out):
                line = out[y]
                out[y] = (slice_ansi(line, 0, left) + RESET + part + RESET
                          + slice_ansi(line, left + bw, float("inf")))
        return out

    def kanban_selected(self):
        columns = self.kanban_columns()
        if not columns:
            return None
        self.kcol = max(0, min(self.kcol, len(columns) - 1))
        rows = columns[self.kcol][2]
        self.krow = max(0, min(self.krow, len(rows) - 1)) if rows else 0
        return rows[self.krow] if rows else None

    def move_card(self, row, column_index):
        # Pinned pins; a group of yours puts it in the group; a state column takes it out of
        # the group and the pin, and the session falls in the column of its state.
        columns = self.kanban_columns()
        if not row or not 0 <= column_index < len(columns):
            return
        key, title, _ = columns[column_index]
        pinned = row["id"] in self.pins()
        if key == "pinned":
            if not pinned:
                self.toggle_pin(row)
        else:
            if pinned:
                self.toggle_pin(row)
            self.set_group([row["id"]], key[len("custom:"):] if key.startswith("custom:") else "")
        self.notice = f"{row['name']} → {title}"
        self.follow(row["id"])

    def chip(self, row):
        """Card state badge: what the session expects from you."""
        if row["busy"]:
            return "\x1b[38;2;90;150;235m◌ working" + RESET, 13
        if row["group"] == "needs":
            return YELLOW + BOLD + "● needs you" + RESET, 17
        return GREEN + "✓ done" + RESET, 10

    def cmd_menu(self):
        """/ commands that match what you typed (only at the start of the reply)."""
        text = self.prompt
        if self.mode != "reply" or not self.reply_to or not text.startswith("/") or " " in text or "\n" in text:
            return []
        typed = text[1:].lower()
        names = slash_commands(self.reply_to.get("cwd"))
        # Plus those the session itself says it accepts (mods, plugins, outside skills), without description.
        known = {n for n, _ in names}
        names = sorted(names + [(n, "") for n in session_commands(self.reply_to) if n not in known])
        hits = [c for c in names if c[0].lower().startswith(typed)] + \
               [c for c in names if typed and typed in c[0].lower() and not c[0].lower().startswith(typed)]
        if typed != getattr(self, "cmd_typed", None):
            self.cmd_typed, self.cmd_sel = typed, 0
        return hits

    def with_images(self, text):
        """Replaces each [Imagem #n] with the file path, so the session opens the image."""
        images, self.images = getattr(self, "images", {}), {}
        return re.sub(r"\[Imag(?:em|e) #(\d+)\]",
                      lambda m: f"[Image #{m.group(1)}: {images[m.group(1)]}]" if m.group(1) in images else m.group(0),
                      text)

    def paste_image(self, item):
        """Alt+V: saves the copied image to a file and puts the path in the text. The session
        receives the path and opens the image, as with its own Alt+V."""
        folder = os.path.normpath(os.path.join(HOME, "jobs", "agents-images"))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("image-%Y%m%d-%H%M%S.png"))
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing;"
            "$img = [System.Windows.Forms.Clipboard]::GetImage();"
            f"if ($img) {{ $img.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png); 'ok' }}"
            "else { $f = [System.Windows.Forms.Clipboard]::GetFileDropList();"
            " if ($f.Count -gt 0) { 'file:' + $f[0] } }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=10,
                                 creationflags=0x08000000).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out.startswith("file:"):
            path = out[5:].strip()
        elif out != "ok":
            self.notice = "no copied image · copy an image (Win+Shift+S) and press alt+v"
            return
        if (self.view in ("kanban", "split") and self.mode == "task" and not self.prompt and not self.compose_new
                and item and item[0] == "row"):
            self.mode, self.reply_to = "reply", self.reply_target(item[1])
        n = len(re.findall(r"\[Imag(?:em|e) #\d+", self.prompt)) + 1
        # In the box only [Imagem #1] shows, as in the session; the path goes in on send.
        self.images = dict(getattr(self, "images", {}), **{str(n): path})
        self.prompt += ("" if not self.prompt or self.prompt.endswith(" ") else " ") + f"[Image #{n}] "
        self.notice = f"image {n} pasted"

    def recall(self, code):
        """↑ / ↓ in the reply box, as inside the session: with the box empty (or with a
        prompt that came from here, untouched), ↑ brings your previous prompt and ↓ the next."""
        row = self.reply_to
        if code not in ("H", "P") or self.mode != "reply" or not isinstance(row, dict):
            return False
        hist = getattr(self, "hist", None)
        mine = hist and hist["id"] == row["id"] and 0 <= hist["i"] < len(hist["list"]) \
            and self.prompt == hist["list"][hist["i"]]
        if self.prompt and not mine:
            return False
        if not mine:
            seen = []
            for p in all_prompts(row.get("transcript")):
                if p.strip() and (not seen or seen[-1] != p):
                    seen.append(p)
            hist = self.hist = {"id": row["id"], "list": seen, "i": len(seen)}
        i = hist["i"] + (-1 if code == "H" else 1)
        if i < 0 or not hist["list"]:
            return True   # already on the first: stays
        hist["i"] = min(i, len(hist["list"]))
        text = hist["list"][hist["i"]] if hist["i"] < len(hist["list"]) else ""
        # A pasted image comes back as [Image #1] in the box; the path goes along when sending.
        self.images = {m.group(1): m.group(0).split(": ", 1)[1][:-1] for m in IMAGE_TAG.finditer(text)}
        self.prompt = prompt_text(text)
        hist["list"][hist["i"]:hist["i"] + 1] = [self.prompt] if text else []
        self.caret = None
        return True

    def answer_by_number(self, row, text):
        """With pending questions, "1 2" becomes the written answer: one option per question."""
        fresh = next((r for r in self.rows if r["id"] == row["id"]), row)
        asks = fresh.get("questions") or []
        picks = text.replace(",", " ").split()
        if not asks or not picks or not all(p.isdigit() for p in picks) or len(picks) != len(asks):
            return text
        lines = []
        for q, pick in zip(asks, picks):
            options = [o.get("label", "") if isinstance(o, dict) else str(o) for o in q.get("options") or []]
            n = int(pick)
            if not 1 <= n <= len(options):
                return text
            lines.append(f"{q['question']} → {options[n - 1]}")
        return "My answers:\n" + "\n".join(lines)

    def question(self, row):
        """Last part of what Claude wrote: it is the question when it is waiting for you."""
        if row.get("permission"):
            return "asks permission: " + row["permission"]
        if row.get("questions"):
            asks = row["questions"]
            return asks[0]["question"] + (f" (+{len(asks) - 1} question{'s' if len(asks) > 2 else ''})" if len(asks) > 1 else "")
        text = clean_markdown(row.get("last_text") or "")
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        return " ".join((parts[-1] if parts else "").split())

    def card(self, row, w, selected, dragging):
        """A 7-line card of width w (border included)."""
        needs = row["group"] == "needs" and not row["busy"]
        edge = ORANGE if selected or dragging else DIM
        inner = w - 4
        summary = (self.question(row) if needs else "") or row["summary"] or "—"
        first, rest, used = "", "", 0
        for word in summary.split(" "):
            if text_width(first) + text_width(word) + 1 <= inner and not rest:
                first = (first + " " + word).strip()
            else:
                rest = (rest + " " + word).strip()
        badge, badge_len = self.badge(row)
        when = duration(row["elapsed"])
        gap = max(1, inner - badge_len - text_width(when))
        chip, chip_len = self.chip(row)
        new = self.unseen(row) and row["group"] != "needs"
        name = fit(row["name"], max(4, inner - 3 - chip_len - (2 if new else 0)))
        if new:
            name = ESC + "38;2;90;150;235m" + "◆ " + RESET + name
        name = BOLD + name + RESET if selected else name
        pad = " "   # fit() already pads the name to the right width
        if self.peek:
            # With the conversation beside it, the card keeps only the essentials: name, state,
            # a line of what is happening, and the time.
            body = [
                f"{self.marker(row)} {name}{pad}{chip}",
                f"{DIM}{fit(summary, max(4, inner - text_width(when) - 1))} {when}{RESET}",
            ]
        else:
            body = [
                f"{self.marker(row)} {name}{pad}{chip}",
                f"{DIM}{fit(short_model(row['model']) + ' · ' + (row['effort'] or '—'), inner)}{RESET}",
                fit(first, inner),
                f"{DIM}{fit(rest, inner)}{RESET}",
                f"{badge}{' ' * gap}{DIM}{when}{RESET}" if badge_len + text_width(when) < inner else fit(when, inner, "right"),
            ]
        top = f"{edge}╭{'─' * (w - 2)}╮{RESET}"
        bottom = f"{edge}╰{'─' * (w - 2)}╯{RESET}"
        return [top] + [f"{edge}│{RESET} {line} {edge}│{RESET}" for line in body] + [bottom]

    def kanban_lines(self, cols, room, top_y):
        """Board lines and the clickable areas (self.hits)."""
        columns = self.kanban_columns()
        selected = self.kanban_selected()
        n = len(columns)
        w = max(30, min(70, (cols - (n - 1)) // max(1, n)))
        fits = max(1, (cols + 1) // (w + 1))
        w = max(w, min(70, (cols - (fits - 1)) // fits))   # visible columns fill the whole width
        if self.kcol < self.kstart:
            self.kstart = self.kcol
        if self.kcol >= self.kstart + fits:
            self.kstart = self.kcol - fits + 1
        self.kstart = max(0, min(self.kstart, max(0, n - fits)))
        shown = list(enumerate(columns))[self.kstart:self.kstart + fits]
        card_h = 4 if self.peek else 7   # with the conversation beside it, short cards (more fit)
        per_col = max(1, (room - 2) // card_h)
        self.hits = []
        blocks = []
        for slot, (ci, (key, title, rows)) in enumerate(shown):
            x0 = 1 + slot * (w + 1)
            dot = COLUMN_DOTS[ci % len(COLUMN_DOTS)]
            head_text = fit(f"{title.upper()}", w - 6) + f"{len(rows):>3}"
            is_col = ci == self.kcol
            head = f"{dot} {BOLD if is_col else ''}{head_text}{RESET} "
            block = [head, (ORANGE if is_col else DIM) + "─" * w + RESET]
            start = self.kscroll.get(key, 0)
            if is_col and rows:
                if self.krow < start:
                    start = self.krow
                if self.krow >= start + per_col:
                    start = self.krow - per_col + 1
            start = max(0, min(start, max(0, len(rows) - per_col)))
            self.kscroll[key] = start
            for ri in range(start, min(len(rows), start + per_col)):
                r = rows[ri]
                y0 = top_y + len(block)
                is_sel = selected is not None and is_col and r["id"] == selected["id"]
                dragging = self.drag is not None and self.drag[2] == r["id"]
                block += self.card(r, w, is_sel, dragging)
                self.hits.append((x0, x0 + w - 1, y0, y0 + card_h - 1, ci, ri))
            if not rows:
                block.append(f"{DIM}{fit('  (vazio)', w)}{RESET}")
            if len(rows) > start + per_col:
                block.append(f"{DIM}{fit(f'  ↓ {len(rows) - start - per_col} more', w)}{RESET}")
            blocks.append((x0, ci, block))
        self.columns_at = [(x0, x0 + w - 1, ci) for x0, ci, _ in blocks]
        height = max((len(b) for _, _, b in blocks), default=0)
        out = []
        for i in range(height):
            parts = []
            for _, _, block in blocks:
                line = block[i] if i < len(block) else ""
                parts.append(line + " " * max(0, w - visible_width(line)))
            out.append(" ".join(parts))
        if n > fits:
            out.append(f"{DIM}  columns {self.kstart + 1}–{self.kstart + len(shown)} of {n} · ←/→ to see the others{RESET}")
        return out


    # ------------------------------------------------ projects (Paperclip style)

    def board_data(self):
        data = read_json(TASKS_FILE, {})
        data = data if isinstance(data, dict) else {}
        goals = [g for g in data.get("goals") or [] if isinstance(g, dict) and g.get("id")]
        tasks = [t for t in data.get("tasks") or [] if isinstance(t, dict) and t.get("id")]
        return goals, tasks

    def save_board(self, goals, tasks):
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump({"goals": goals, "tasks": tasks}, f, indent=2, ensure_ascii=False)

    def update_task(self, task_id, **changes):
        goals, tasks = self.board_data()
        for t in tasks:
            if t["id"] == task_id:
                t.update(changes)
        self.save_board(goals, tasks)

    def goal_selected(self):
        goals, _ = self.board_data()
        self.bgoal = max(0, min(self.bgoal, len(goals)))
        return goals[self.bgoal - 1] if self.bgoal else None

    def task_agents(self, task):
        by_id = {r["id"]: r for r in self.rows}
        return [by_id[i] for i in task.get("sessions", []) if i in by_id]

    def task_column(self, task):
        if task.get("done"):
            return "done"
        agents = self.task_agents(task)
        if not agents:
            return "todo"
        return "working" if any(a["busy"] for a in agents) else "review"

    def task_tokens(self, task):
        return sum(a["tokens"] for a in self.task_agents(task))

    def board_columns(self):
        _, tasks = self.board_data()
        goal = self.goal_selected()
        if goal:
            tasks = [t for t in tasks if t.get("goal") == goal["id"]]
        if self.find:
            needle = self.find.lower()
            tasks = [t for t in tasks if needle in (t.get("title", "") + " " + t.get("body", "")).lower()]
        return [(key, title, [t for t in tasks if self.task_column(t) == key]) for key, title in BOARD]

    def board_selected(self):
        columns = self.board_columns()
        self.bcol = max(0, min(self.bcol, len(columns) - 1))
        tasks = columns[self.bcol][2]
        self.brow = max(0, min(self.brow, len(tasks) - 1)) if tasks else 0
        return tasks[self.brow] if tasks else None

    def follow_task(self, task_id):
        for ci, (_, _, tasks) in enumerate(self.board_columns()):
            for ri, t in enumerate(tasks):
                if t["id"] == task_id:
                    self.bcol, self.brow = ci, ri

    def new_goal(self, text):
        goals, tasks = self.board_data()
        goal = {"id": os.urandom(3).hex(), "title": " ".join(text.split()), "project": self.new_session_dir()}
        self.save_board(goals + [goal], tasks)
        self.bgoal, self.bside = len(goals) + 1, False
        self.notice = f"goal {goal['title']} created · now type its tasks"

    def new_task(self, text):
        goals, tasks = self.board_data()
        goal = self.goal_selected()
        project = (goal or {}).get("project") or self.new_session_dir()
        title, _, body = text.strip().partition("\n")
        task = {"id": os.urandom(3).hex(), "title": title.strip(), "body": body.strip(), "goal": (goal or {}).get("id", ""),
                "project": project, "sessions": [], "done": False, "created": int(time.time() * 1000)}
        self.save_board(goals, tasks + [task])
        self.notice = f"task created in {os.path.basename(project)} · enter launches an agent on it"
        self.follow_task(task["id"])

    def launch_agent(self, task):
        """Launches an agent (claude --bg) in the task folder, with the goal and the task in the request."""
        self.notice, self.notice_until = f"launching an agent on {task['title']}…", float("inf")
        goals, _ = self.board_data()
        goal = next((g for g in goals if g["id"] == task.get("goal")), None)
        prompt = (f"Goal: {goal['title']}\n\n" if goal else "") + task["title"] + (
            "\n\n" + task["body"] if task.get("body") else "")
        ran = subprocess.run(["claude", "--bg", "-n", task["title"][:40], prompt], capture_output=True, text=True,
                             encoding="utf-8", cwd=task.get("project") or None)
        out = (ran.stdout or "") + (ran.stderr or "")
        got = re.search(r"backgrounded · (\w+)", out)
        if ran.returncode == 0 and got:
            _, tasks = self.board_data()
            current = next((t for t in tasks if t["id"] == task["id"]), task)
            self.update_task(task["id"], sessions=current.get("sessions", []) + [got.group(1)], done=False)
            self.notice = f"agent {got.group(1)} working on {task['title']}"
        else:
            self.notice = "error: " + (out.strip().splitlines() or [f"exit {ran.returncode}"])[0]
        self.notice_until = time.time() + 15
        self.refresh()

    def move_task(self, task, column_index):
        # The middle columns follow the agents; moving only checks or unchecks "Done".
        if not task or not 0 <= column_index < len(BOARD):
            return
        if BOARD[column_index][0] == "done":
            self.update_task(task["id"], done=True)
            self.notice = f"{task['title']} → Feito"
        elif task.get("done"):
            self.update_task(task["id"], done=False)
            self.notice = f"{task['title']} moved out of Done"
        else:
            self.notice = "the column follows the agents · enter launches an agent · shift+→ to Done marks it done"
        self.follow_task(task["id"])

    def task_card(self, task, w, selected):
        edge = ORANGE if selected else DIM
        inner = w - 4
        agents = self.task_agents(task)
        last = agents[-1] if agents else None
        title = fit(task.get("title") or "—", inner)
        where = os.path.basename(task.get("project", "")) or "—"
        if last:
            owner = f"dono {last['name']}" + (f" +{len(agents) - 1}" if len(agents) > 1 else "")
            status = f"{self.marker(last)} {fit(last['summary'] or '—', inner - 2)}"
            if any(a["group"] == "needs" for a in agents):
                left, left_len = YELLOW + "needs you" + RESET, text_width("needs you")
            else:
                left, left_len = DIM + tokens_label(self.task_tokens(task)) + RESET, text_width(tokens_label(self.task_tokens(task)))
            when = duration(last["elapsed"])
            foot = left + " " * max(1, inner - left_len - text_width(when)) + DIM + when + RESET
        else:
            owner = "no owner · enter launches an agent"
            status = DIM + fit(task.get("body") or "", inner) + RESET
            foot = ""
        body = [
            BOLD + title + RESET if selected else title,
            f"{DIM}{fit(where + ' · ' + owner, inner)}{RESET}",
            status,
            foot,
        ]
        top = f"{edge}╭{'─' * (w - 2)}╮{RESET}"
        bottom = f"{edge}╰{'─' * (w - 2)}╯{RESET}"
        return [top] + [f"{edge}│{RESET} {fit_ansi(line, inner)} {edge}│{RESET}" for line in body] + [bottom]

    def goals_panel(self, width, top_y):
        """Left column: goals with progress (done/total) and cost."""
        goals, tasks = self.board_data()
        self.goal_selected()
        self.shits = []
        out = [f"{BOLD}METAS{RESET}", (ORANGE if self.bside else DIM) + "─" * width + RESET]
        entries = [(None, "All tasks", tasks)] + [(g, g["title"], [t for t in tasks if t.get("goal") == g["id"]]) for g in goals]
        for i, (goal, title, items) in enumerate(entries):
            done = sum(1 for t in items if t.get("done"))
            mark = "▸ " if i == self.bgoal else "  "
            count = f"{done}/{len(items)}"
            head = fit(mark + title, width - len(count) - 1) + " " + count
            if i == self.bgoal:
                head = (BAR + BOLD if self.bside else BOLD) + head + RESET
            filled = round(10 * done / len(items)) if items else 0
            bar = f"  {GREEN}{'━' * filled}{RESET}{DIM}{'━' * (10 - filled)}{RESET}"
            cost = tokens_label(sum(self.task_tokens(t) for t in items))
            where = os.path.basename(goal.get("project", "")) if goal else ""
            self.shits.append((1, width, top_y + len(out), top_y + len(out) + 1, i))
            out += [head, bar + f" {DIM}{fit(cost + ('  ' + where if where else ''), width - 13)}{RESET}", ""]
        out.append(f"{DIM}{fit('← here, type and press enter', width)}{RESET}")
        out.append(f"{DIM}{fit('creates a new goal', width)}{RESET}")
        return out

    def board_lines(self, cols, room, top_y):
        side_w = 30
        side = self.goals_panel(side_w, top_y)
        columns = self.board_columns()
        selected = self.board_selected()
        n = len(columns)
        w = max(20, (cols - side_w - 5 - (n - 1)) // n)  # 1 + side + " │ " + columns fits in cols - 1
        per_col = max(1, (room - 3) // 6)
        self.bhits = []
        blocks = []
        for ci, (key, title, tasks) in enumerate(columns):
            x0 = side_w + 4 + ci * (w + 1)
            dot = COLUMN_DOTS[ci % len(COLUMN_DOTS)]
            is_col = ci == self.bcol and not self.bside
            head = f"{dot} {BOLD if is_col else ''}{fit(title.upper(), w - 6)}{len(tasks):>3}{RESET} "
            block = [head, (ORANGE if is_col else DIM) + "─" * w + RESET]
            start = self.bscroll.get(key, 0)
            if ci == self.bcol and tasks:
                start = max(min(start, self.brow), self.brow - per_col + 1)
            start = max(0, min(start, max(0, len(tasks) - per_col)))
            self.bscroll[key] = start
            for ri in range(start, min(len(tasks), start + per_col)):
                y0 = top_y + len(block)
                is_sel = is_col and selected is not None and tasks[ri]["id"] == selected["id"]
                block += self.task_card(tasks[ri], w, is_sel)
                self.bhits.append((x0, x0 + w - 1, y0, y0 + 5, ci, ri))
            if not tasks:
                block.append(f"{DIM}{fit('  (vazio)', w)}{RESET}")
            if len(tasks) > start + per_col:
                block.append(f"{DIM}{fit(f'  ↓ {len(tasks) - start - per_col} more', w)}{RESET}")
            blocks.append(block)
        height = max([len(side)] + [len(b) for b in blocks])
        out = []
        for i in range(min(height, room)):
            left = side[i] if i < len(side) else ""
            right = " ".join((b[i] if i < len(b) else "") + " " * max(0, w - visible_width(b[i] if i < len(b) else ""))
                             for b in blocks)
            out.append(" " + left + " " * max(0, side_w - visible_width(left)) + f" {DIM}│{RESET} " + right)
        return out


    # ------------------------------------------------ focus (one session at a time, with the "where we stopped")

    def save_config(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"view": self.view, "seen": self.seen, "peek": self.peek, "names": self.names,
                       "alerts": self.alerts, "features": self.features}, f)

    def on(self, key):
        """Is the feature on? (⊞ Features panel; everything on by default)"""
        return self.features.get(key, True) is not False

    def mark_seen(self, row_id):
        self.seen[row_id] = int(time.time() * 1000)
        self.save_config()

    def unseen(self, row):
        return not row["busy"] and row["updated"] > self.seen.get(row["id"], 0)

    def focus_queue(self):
        """Queue: who needs you (the longest-waiting first), then the ones
        that finished with news, and last the ones that are working."""
        rows = self.rows
        if self.find:
            needle = self.find.lower()
            rows = [r for r in rows if needle in r["name"].lower() or needle in r["summary"].lower()]
        needs = sorted([r for r in rows if r["group"] == "needs" and not r["busy"]], key=lambda r: r["updated"])
        news = sorted([r for r in rows if r["group"] == "done" and self.unseen(r)], key=lambda r: -r["updated"])
        busy = [r for r in rows if r["busy"]]
        return needs + news + busy

    def focus_selected(self):
        queue = self.focus_queue()
        if not queue:
            return None
        row = next((r for r in queue if r["id"] == self.fid), None) or queue[0]
        self.fid = row["id"]
        return row

    def focus_step(self, step):
        queue = self.focus_queue()
        if not queue:
            return
        current = self.focus_selected()
        ids = [r["id"] for r in queue]
        at = ids.index(current["id"]) if current and current["id"] in ids else 0
        self.fid = ids[(at + step) % len(ids)]
        self.pscroll = 0
        # Pick the next one before marking: the view leaves the queue and would change the positions.
        if current and not current["busy"]:
            self.mark_seen(current["id"])

    def focus_events(self, row):
        """What happened since the last time you looked (or the last steps)."""
        events = []
        for e in timeline(row["id"]):
            detail = " ".join(str(e.get("detail") or "").split())
            if not detail:
                continue
            if e.get("text"):
                events.append((e["ms"], "claude", detail))
            elif e.get("state") != "working":
                events.append((e["ms"], "you", detail))
        dedup = []
        for ev in events:
            if not dedup or dedup[-1][1:] != ev[1:]:
                dedup.append(ev)
        seen = self.seen.get(row["id"], 0)
        fresh = [e for e in dedup if e[0] > seen]
        return (fresh if fresh else dedup[-4:]), bool(fresh)

    def focus_lines(self, cols, room, top_y):
        queue = self.focus_queue()
        row = self.focus_selected()
        waiting = sum(1 for r in queue if r["group"] == "needs" and not r["busy"])
        news = sum(1 for r in queue if r["group"] == "done")
        out = [f" {BOLD}QUEUE{RESET} {DIM}{waiting} need you · {news} with news · ←/→ switch{RESET}"]
        # Queue strip: the chosen one highlighted, the rest around it.
        chips = []
        for r in queue:
            color = YELLOW if r["group"] == "needs" and not r["busy"] else GREEN if not r["busy"] else DIM
            label = f" ● {fit(r['name'], 18).rstrip()} "
            chips.append((r["id"], color, label))
        strip, used = " ", 1
        at = next((i for i, c in enumerate(chips) if c[0] == (row or {}).get("id")), 0)
        start = max(0, at - 3)
        for rid, color, label in chips[start:]:
            if used + text_width(label) > cols - 2:
                strip += DIM + "…" + RESET
                break
            strip += (BAR + BOLD + label + RESET) if row and rid == row["id"] else (color + label + RESET)
            used += text_width(label)
        out += [strip, ""]
        if not row:
            out.append(f"  {GREEN}✓ nothing waiting for you.{RESET} {DIM}Sessions that finish show up here.{RESET}")
            return out
        return out + self.focus_box(row, cols - 2, room - len(out))

    def focus_box(self, row, width, height, edge=None, compact=False, typing=None):
        """Box with the session's "where we stopped": used in Focus and in the Kanban pop-up.
        compact (pop-up) cuts the steps and shortens the answer; typing shows the
        reply box inside it."""
        inner = width - 4
        chip, _ = self.chip(row)
        where = os.path.basename(row.get("cwd") or "")
        body = [f"{self.marker(row)} {BOLD}{row['name']}{RESET}  {chip}", ""]

        def section(title, color=ORANGE):
            body.append(color + BOLD + title + RESET)

        if row.get("intent"):
            section("OBJETIVO")
            body += [DIM + l + RESET for l in wrap(row["intent"], inner)[:1 if compact else 2]]
            body.append("")
        events, fresh = self.focus_events(row)
        # Summary of the last task: what you asked last and what it did.
        asked = next((text for _, who, text in reversed(events) if who == "you"), "")
        if not asked:
            asked = next((" ".join(str(e.get("detail") or "").split()) for e in reversed(timeline(row["id"]))
                          if not e.get("text") and e.get("state") != "working" and e.get("detail")), "")
        if asked:
            section("YOU ASKED")
            body += [ESC + "38;2;90;150;235m" + l + RESET for l in wrap(asked, inner)[:2]]
        if row["summary"]:
            section("ELE FEZ")
            body += wrap(row["summary"], inner)[:2]
        if asked or row["summary"]:
            body.append("")
        if events and not compact:
            section("SINCE YOU LAST LOOKED" if fresh else "LATEST STEPS")
            for _, who, text in events[-6:]:
                tag = (ESC + "38;2;90;150;235m" + "you   " + RESET) if who == "you" else (DIM + "claude" + RESET)
                body.append(f"{tag} {fit(text, inner - 7)}")
            body.append("")
        needs = row["group"] == "needs" and not row["busy"]
        question = self.question(row) if needs else ""
        reply = clean_markdown(row.get("last_text") or "")
        reply = "\n".join(l for l in reply.split("\n") if l.strip().strip("`") or not l.strip())
        if question:
            # The question already goes below, highlighted: it leaves the end of the answer.
            paras = [p for p in re.split(r"\n\s*\n", reply) if p.strip()]
            if paras and " ".join(paras[-1].split()) == question:
                reply = "\n\n".join(paras[:-1])
        if reply.strip():
            section("LATEST REPLY")
            lines = [l for para in reply.split("\n") for l in (wrap(para, inner) if para.strip() else [""])]
            keep = max(3, height - 2 - len(body) - (4 if question else 1) - (2 if typing is not None else 0))
            if compact:
                keep = min(keep, 8)
            if len(lines) > keep:
                # PgUp/PgDn scroll the answer (self.pscroll = lines above the end).
                self.pscroll = max(0, min(self.pscroll, len(lines) - (keep - 1)))
                end = len(lines) - self.pscroll
                start = max(0, end - (keep - 1))
                mark = f"↑ {start} more lines · pgup/pgdn scrolls" if start else "↓ pgdn back to the end"
                lines = [DIM + mark + RESET] + lines[start:end]
            else:
                self.pscroll = 0
            body += lines
            body.append("")
        if question:
            section("NEEDS YOU", YELLOW)
            body += [YELLOW + l + RESET for l in wrap(question, inner)[:3]]
        while body and not body[-1]:
            body.pop()
        if typing is not None:
            # Reply box inside the pop-up: what you type shows up here.
            body = body[: max(1, height - 4)]
            body.append(DIM + "─" * inner + RESET)
            if typing:
                body.append(f"❯ {typing.replace(chr(10), ' ⏎ ')}{ESC}7m {RESET}")
            else:
                body.append(f"❯ {DIM}type here and press enter to reply to {row['name']}{RESET}")
        edge = edge or (YELLOW if needs else DIM)
        box = [f" {edge}╭{'─' * (width - 2)}╮{RESET}"]
        for line in body[: max(1, height - 2)]:
            box.append(f" {edge}│{RESET} {fit_ansi(line, width - 4)} {edge}│{RESET}")
        box.append(f" {edge}╰{'─' * (width - 2)}╯{RESET}")
        return box

    # ------------------------------------------------ screen items

    def sections(self):
        pins = self.pins()
        order, grouped = self.custom()
        rows = self.rows
        if self.find:
            needle = self.find.lower()
            inside = self.find_hits if self.hits_for == self.find else {}
            rows = [r for r in rows if needle in r["name"].lower() or needle in r["summary"].lower() or r["id"] in inside]
        # At the end of the list (the top belongs to your backlog groups), the ones that finished and you have not
        # looked at yet: they leave the other groups until you stop on them, so none finishes hidden.
        check = [r for r in rows if r["id"] in self.alerts and not r["busy"] and r["group"] != "needs"] \
            if self.on("check_group") else []
        rows = [r for r in rows if r not in check]
        by_id = {r["id"]: r for r in rows}
        out = [("pinned", [by_id[p] for p in pins if p in by_id])]
        # Your groups come right after the pinned ones; the session leaves the state group.
        for name in order:
            out.append(("custom:" + name, [r for r in rows if grouped.get(r["id"]) == name and r["id"] not in pins]))
        for key, _ in GROUPS[1:]:
            out.append((key, [r for r in rows if r["group"] == key and r["id"] not in pins and r["id"] not in grouped]))
        out.append(("check", check))
        return [(k, items) for k, items in out if items]

    def items(self):
        """Titles and lines in screen order: ('head', group) or ('row', line)."""
        out = []
        for key, rows in self.sections():
            out.append(("head", key, rows))
            if key not in self.collapsed:
                out += [("row", r, None) for r in rows]
        return out

    def current(self):
        if self.view == "focus":
            row = self.focus_selected()
            return ("row", row, None) if row else None
        if self.view == "board":
            task = None if self.bside else self.board_selected()
            return ("task", task, None) if task else None
        if self.view == "grid":
            row = self.grid_selected()
            return ("row", row, None) if row else None
        if self.view == "kanban":
            row = self.kanban_selected()
            return ("row", row, None) if row else None
        items = self.items()
        if not items:
            return None
        self.cursor = max(0, min(self.cursor, len(items) - 1))
        return items[self.cursor]

    # ------------------------------------------------ drawing

    def dialog_of(self, row):
        """The question or permission open in the session, read from its screen; opens the
        hidden connection the first time."""
        panel = getattr(self, "panel", None)
        is_panel = bool(row and panel and panel["id"] == row["id"])   # menu opened by a / command
        if not row or not row.get("alive") or (not is_panel and (row["busy"] or not (row.get("questions") or row.get("permission")))):
            return []
        conn = _attached.get(row["id"])
        if is_panel and conn and not conn.dead.is_set():
            lines = conn.dialog() if conn.ready.is_set() else []
            if lines:
                panel["seen"] = True
            elif panel["seen"] or time.time() - panel["t"] > 6:   # closed (or the command had no menu)
                self.panel = None
            conn.used = time.time()
            return lines
        if conn is not None and not conn.dead.is_set() and not _safe(conn.p.isalive):
            conn.dead.set()   # the hidden terminal closed: its screen is stale, open another
        if conn is None or conn.dead.is_set():
            if getattr(self, "warming", None) != row["id"] or conn is not None \
                    or time.time() - getattr(self, "warming_t", 0) > 30:
                self.warming, self.warming_t = row["id"], time.time()
                self.background(lambda r: _safe(attach_open, r), row)
            return []
        conn.used = time.time()
        return conn.dialog() if conn.ready.is_set() else []

    def mode_of(self, row):
        """The session mode, colored as in Claude. Its screen wins (changes instantly
        with shift+tab); with no open connection, the last mode saved in the conversation."""
        conn = _attached.get(row["id"])
        name = conn.mode() if conn and not conn.dead.is_set() else ""
        if not name:
            name = MODE_NAMES.get((_meta.get(row.get("transcript")) or {}).get("mode"), "")
        name = name.replace(" on", "")
        if not name:
            return ""
        color = {"plan mode": "38;2;72;150;140m", "accept edits": "38;2;175;135;255m",
                 "auto mode": "38;2;215;119;87m", "bypass permissions": "38;2;232;98;98m"}.get(name)
        icon = "⏸" if name in ("plan mode", "manual mode") else "⏵⏵"
        return (ESC + color if color else DIM) + f"{icon} {name}" + RESET

    def switch_mode(self, row):
        """shift+tab: switches the chosen session's mode, like shift+tab inside it."""
        conn = _safe(attach_open, row)
        if not conn or not conn.ready.wait(15) or conn.dialog() or conn.waiting_you():
            self.notice, self.notice_until = f"{row['name']}: could not switch the mode now", time.time() + 4
            return
        conn.press("\x1b[Z")
        time.sleep(0.5)
        self.notice, self.notice_until = f"{row['name']}: {conn.mode() or 'mode switched'}", time.time() + 4

    def compact_line(self, meta, inner):
        """While /compact runs: "◆ compacting ━━━━━━──── 45% · 17s of ~38s". Claude Code does not
        report progress; the estimate is by time: the context being compacted times how long this
        session took per token last time (with no history, the average of 577k in 38 s)."""
        since = stamp(meta.get("compacting"), 0) / 1000 if meta.get("compacting") else 0
        elapsed = time.time() - since
        if not since or elapsed > 900:   # 15 min without the mark: the /compact did not work
            return ""
        expected = max(8.0, (meta.get("compact_from") or 0) * (meta.get("compact_rate") or COMPACT_RATE))
        # Up to the estimate the bar moves straight; past it, it slows towards 99% (never gets there alone).
        pct = elapsed / expected * 0.9 if elapsed < expected else 0.9 + 0.09 * (1 - expected / elapsed)
        tail = f" {round(pct * 100)}% · {duration_short(elapsed)} of ~{duration_short(expected)}"
        head = f"{SPINNER[self.tick % len(SPINNER)]} Compacting the conversation "
        width = max(10, min(40, inner - text_width(head) - text_width(tail) - 2))
        done = round(width * pct)
        return (ORANGE + head + "━" * done + ESC + f"38;2;{CTX_FREE[0]};{CTX_FREE[1]};{CTX_FREE[2]}m"
                + "━" * (width - done) + RESET + DIM + tail + RESET)

    def turn_line(self, row, meta):
        """The line at the end of the conversation, in Claude's format and always from the same account (the
        saved conversation), so it does not change look when switching sessions:
        working "✻ Working… (3m 55s · ↓ 3.8k tokens)" · stopped "✻ Worked for 16s · done 18:53"."""
        start = stamp(meta.get("turn_start"), 0) / 1000 if meta.get("turn_start") else 0
        # Waiting for you on an open question or permission: the turn has not ended and the request
        # already shows at the end. Stopped with a question in the text ("Shall I start?"), the line counts.
        if not start or row.get("questions") or row.get("permission"):
            return ""
        tokens = meta.get("turn_tokens") or 0
        tok = f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)
        verbs = ("Working", "Thinking", "Cooking", "Brewing", "Pondering", "Crafting", "Churning", "Baking")
        verb = verbs[int(start) % len(verbs)]   # the same verb throughout the turn
        extra = f" · {row['background']} running in background" if row.get("background") else ""
        kinds = [f["kind"] for f in row.get("fan") or []]
        if row.get("background") and kinds:   # "1 agent, 1 shell running in background"
            names = {"agent": ("agent", "agents"), "shell": ("shell", "shells")}
            parts = [f"{kinds.count(k)} {names.get(k, (k, k + 's'))[kinds.count(k) > 1]}" for k in dict.fromkeys(kinds)]
            extra = " · " + ", ".join(parts) + " running in background"
        if row.get("background"):
            # "claude -p" that a session script opened: Claude only sees the shell that opened them.
            n = len(cached(("headless", row.get("cwd"), row.get("transcript")), 10, lambda: headless_agents(row))) if self.on("claude_p") else 0
            if n:
                extra += f" · {n} claude -p working"
        if row["busy"]:
            spin = SPINNER[self.tick % len(SPINNER)]
            text = f"{spin} {verb}… ({duration_short(time.time() - start)} · ↓ {tok} tokens){extra}"
            return ORANGE + text + RESET + DIM + "  " + fit(row["summary"], max(0, self.pane_inner - text_width(text) - 2)) + RESET
        end = stamp(meta.get("turn_end"), 0) / 1000 if meta.get("turn_end") else 0
        if not end or end < start:
            return ""
        past = {"Working": "Worked", "Thinking": "Thought", "Cooking": "Cooked", "Brewing": "Brewed",
                "Pondering": "Pondered", "Crafting": "Crafted", "Churning": "Churned", "Baking": "Baked"}[verb]
        return DIM + f"✻ {past} for {duration_short(end - start)} · done {time.strftime('%H:%M', time.localtime(end))}{extra}" + RESET

    def background_lines(self, row, turns, inner):
        """What runs in the background, one line each, like a task list:
        "  ✶ Run full test suite and commit ·········· shell  2m 10s"."""
        colors = {"agent": "38;2;175;135;255m", "shell": "38;2;110;160;220m"}
        out = []
        # Only subagents get a line; commands and watchers only count in the "running in background" tally.
        fan = [f for f in row.get("fan") or [] if f.get("kind") == "agent"]
        # And the "claude -p" that a session script opened (testers, batches): Claude does not see them.
        if self.on("claude_p"):
            fan += [dict(h, kind="claude -p") for h in
                    cached(("headless", row.get("cwd"), row.get("transcript")), 10, lambda: headless_agents(row))]
        colors["claude -p"] = "38;2;215;119;87m"
        for n, f in enumerate(fan[:12]):
            kind = f["kind"]
            spin = SPINNER[(self.tick + n * 2) % len(SPINNER)]
            since = duration_short(time.time() - f["startedAt"] / 1000) if f.get("startedAt") else ""
            right = f"{kind:<9} {since:>7}"
            text = fit(f["label"] if kind == "claude -p" else background_label(f, turns),
                       max(10, inner - len(right) - 8)).rstrip()
            dots = "·" * max(1, inner - 6 - text_width(text) - len(right))
            color = ESC + colors.get(kind, "2m")
            out.append(f"  {color}{spin}{RESET} {text} {DIM}{dots} {RESET}{color}{kind:<9}{RESET} {DIM}{since:>7}{RESET}")
        if len(fan) > 12:
            out.append(DIM + f"    … +{len(fan) - 12} more" + RESET)
        return out

    def answer_question(self, row, text):
        """Answers the open question (AskUserQuestion) through the session screen. "1 2" picks one
        option per question and sends; other text goes in the current question's "Type something."."""
        conn = _attached.get(row["id"])
        if not conn or conn.dead.is_set() or not conn.dialog():
            self.notice, self.notice_until = f"{row['name']}: the question is not open now", time.time() + 5
            return
        # The option may come with a small box in front ("4. [ ] Type something"), in the multi-select one.
        option = lambda pat: next((m.group(1) for l in conn.dialog()
                                   for m in [re.match(r"\s*(?:❯\s*)?(\d+)\.\s*(?:\[.\]\s*)?" + pat, l)] if m), None)
        multi = any(re.match(r"\s*(?:❯\s*)?\d+\.\s*\[.\]", l) for l in conn.dialog())

        def go(want):
            """Moves the ❯ (the marked line) to the line that want accepts, with ↑ / ↓."""
            for _ in range(16):
                lines = conn.dialog()
                at = next((i for i, l in enumerate(lines) if l.lstrip().startswith("❯")), None)
                to = next((i for i, l in enumerate(lines) if want(l)), None)
                if at is None or to is None:
                    return False
                if at == to:
                    return True
                conn.press("\x1b[A" if to < at else "\x1b[B")
                time.sleep(0.4)
            return False
        is_submit = lambda l: l.replace("❯", "").strip() == "Submit"
        picks = text.replace(",", " ").split()
        if picks and all(p.isdigit() for p in picks):
            for p in picks:   # in multi-select, each number toggles the box
                conn.press(p)
                time.sleep(0.8)
            if multi and go(is_submit):
                conn.press("\r")
                time.sleep(0.8)
        elif multi:
            # Multi-select: the text goes in the "Type something" line (with the ❯ on it) and the Submit
            # of the question is one line below.
            if not go(lambda l: re.search(r"\d+\.\s*\[.\]\s*Type something", l)):
                self.notice, self.notice_until = f"{row['name']}: could not reach Type something", time.time() + 5
                return
            conn.press("\x1b[200~" + " ".join(text.split()) + "\x1b[201~")
            time.sleep(0.6)
            if go(is_submit):
                conn.press("\r")
                time.sleep(0.8)
        else:
            n = option("Type something")
            if not n:
                self.notice, self.notice_until = f"{row['name']}: this one only takes an option number", time.time() + 5
                return
            conn.press(n)
            time.sleep(0.6)
            conn.press("\x1b[200~" + " ".join(text.split()) + "\x1b[201~")
            time.sleep(0.6)
            conn.press("\r")
            time.sleep(0.8)
        # Answered all: the "Submit" tab remains. Send it, as enter would.
        submit = option("Submit answers")
        if submit:
            conn.press(submit)
            time.sleep(0.8)
        left = bool(conn.dialog())
        self.notice, self.notice_until = (f"{row['name']}: answer typed · next question below" if left
                                          else f"{row['name']}: answered ✓"), time.time() + 5
        self.refresh()

    def press_option(self, conn, key, row):
        ok = conn.press(key)
        if not ok:
            # The hidden connection had died: open another and press again, if the question is still there.
            conn = _safe(attach_open, row)
            ok = bool(conn and conn.ready.wait(15) and conn.dialog() and conn.press(key))
        if ok:
            self.notice, self.notice_until = f"{row['name']}: option {key} ✓", time.time() + 5
        else:
            self.notice, self.notice_until = f"{row['name']}: could not press {key} · try again", time.time() + 5
        time.sleep(1.2)
        self.refresh()

    def ring(self):
        if not self.on("sound") or time.time() - self.rang < 3:
            return
        self.rang = time.time()
        try:
            import winsound
            winsound.PlaySound("SystemNotification", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:
            pass

    def waiting(self):
        """Who is waiting for you: the ones stopped on a question (the longest-waiting
        first) and the ones that just finished and you have not looked at yet."""
        needs = sorted([r for r in self.rows if r["group"] == "needs" and not r["busy"]], key=lambda r: r["updated"])
        done = sorted([r for r in self.rows if r["id"] in self.alerts and r not in needs and not r["busy"]],
                      key=lambda r: self.alerts[r["id"]])
        return needs + done

    def jump_waiting(self):
        queue = self.waiting()
        if not queue:
            self.notice, self.notice_until = "nobody is waiting for you", time.time() + 3
            return
        item = self.current()
        ids = [r["id"] for r in queue]
        here = item[1]["id"] if item and item[0] == "row" else None
        target = ids[(ids.index(here) + 1) % len(ids)] if here in ids else ids[0]
        if self.view == "list":
            self.view = "split"
        self.follow(target)
        self.pscroll = 0

    def marker(self, row):
        # All snowflakes, as in claude agents; the color tells the state. Blinking orange: finished and you have not seen it.
        if row["busy"]:
            return SPINNER[self.tick % len(SPINNER)]
        if row["group"] == "needs":
            return YELLOW + "✻" + RESET   # no bold: in the terminal bold paints the middle white
        if row["id"] in self.alerts and self.on("check_group"):
            # Finished and you have not seen it yet: the orange ✻ lights up and off every ~0.6 s (off is
            # a space; gray looked almost white and dark orange barely showed the blink).
            return (ORANGE + "✻" + RESET) if (self.tick // 5) % 2 == 0 else " "   # no bold: paints the middle white
        if row["alive"]:
            return GREEN + "✻" + RESET
        return DIM + "•" + RESET

    def badge(self, row):
        if len(row["prs"]) == 1:
            return PURPLE + "#" + row["prs"][0] + RESET, 1 + len(row["prs"][0])
        if row["prs"]:
            text = f"{len(row['prs'])} PRs"
            return text, len(text)
        if row["frames"]:
            text = f"{row['frames']} ⧉"
            return ORANGE + text + RESET, len(text)
        return "", 0

    def prewarm(self):
        # You started answering: already opens the session underneath, and Enter sends right away.
        row = self.reply_to if self.mode == "reply" else None
        if isinstance(row, dict) and self.prompt.startswith("/"):
            session_commands(row)   # the full command list arrives while you type
        if isinstance(row, dict) and row.get("alive") and row.get("id") not in _attached \
                and getattr(self, "warming", None) != row["id"]:
            self.warming, self.warming_t = row["id"], time.time()
            self.background(lambda r: _safe(attach_open, r), row)

    def search_loop(self):
        # Searches the conversations for what you typed in ctrl+f, without freezing the screen.
        while True:
            try:
                self._search_once()
            except Exception:
                pass
            time.sleep(0.15)

    def _search_once(self):
        if True:
            wanted = self.find
            sizes = 0
            if len(wanted) >= 3:
                for r in list(self.rows):
                    try:
                        sizes += os.path.getsize(r.get("transcript") or "")
                    except OSError:
                        pass
            if len(wanted) >= 3 and (wanted != self.hits_for or sizes != getattr(self, "hits_sizes", None)):
                time.sleep(0.3)   # waits for you to stop typing
                if self.find != wanted:
                    return
                with self.lock:
                    rows = list(self.rows)
                hits = search_conversations(rows, wanted)
                if self.find == wanted:
                    if wanted != self.hits_for:
                        self.index_sel = None
                    self.find_hits, self.hits_for, self.hits_sizes = hits, wanted, sizes
            elif len(wanted) < 3:
                self.find_hits, self.hits_for = {}, ""

    def hits_of(self, row):
        """The places in this session's conversation where what you search for is."""
        if not row or not self.find or self.hits_for != self.find:
            return []
        return self.find_hits.get(row["id"], [])

    def go_hit(self, row):
        """Goes to the chosen result: clears the search and scrolls the conversation to its prompt."""
        n, word = self.index_sel, self.find
        self.find, self.mode, self.find_hits, self.hits_for = "", "task", {}, ""
        self.follow(row["id"])
        self.index_sel = n
        self.index_go(row)
        self.notice, self.notice_until = f"\"{word}\" · prompt {n}", time.time() + 5

    def hits_list(self, hits, inner, area):
        """In place of the conversation: each place where the word appears; enter or click go there."""
        numbers = [n for n, _, _ in hits]
        if self.index_sel not in numbers:
            self.index_sel = numbers[-1]
        needle = self.find.lower()
        lines = [ORANGE + BOLD + f"FOUND \"{self.find}\" IN {len(hits)} PLACE{'S' if len(hits) > 1 else ''}" + RESET
                 + DIM + "  ctrl+↑↓ pick · enter or click jumps there · esc clears" + RESET, ""]
        rows = max(1, (area - len(lines)) // 2)
        sel = max(i for i, h in enumerate(hits) if h[0] == self.index_sel)
        start = max(0, min(sel - rows // 2, len(hits) - rows))
        where = {}
        for i in range(start, min(len(hits), start + rows)):
            n, who, flat = hits[i]
            at = flat.lower().find(needle)
            text = fit(flat, inner - 4)
            if 0 <= at < len(text) - len(needle):
                text = text[:at] + YELLOW + BOLD + text[at:at + len(needle)] + RESET + text[at + len(needle):]
            head = f"prompt {n} · {'you' if who == 'you' else 'Claude'}"
            mark = i == sel
            where[len(lines)] = n
            lines.append((ESC + "48;2;45;55;72m" + BOLD + "▸ " if mark else "  ") + DIM + head + RESET)
            where[len(lines)] = n
            lines.append("  " + text)
        return lines[:area] + [""] * max(0, area - len(lines)), where

    def sync_reply_target(self, current=None):
        """List + conversation: while typing, the arrow (or click) that picks another session takes the
        message along. The box, the side conversation and the send always stay on the same session."""
        current = current if current is not None else self.current()
        if (self.view == "split" and self.mode == "reply" and isinstance(self.reply_to, dict)
                and not getattr(self, "qediting", None) and current and current[0] == "row"
                and current[1]["id"] != self.reply_to["id"]):
            self.reply_to = current[1]

    def draw(self):
        self.keep_mouse()
        self.prewarm()
        if self.find and not self.searching:
            self.searching = True
            self.background(self.search_loop)
        cols, total = shutil.get_terminal_size((160, 45))
        # The last column stays free: in Windows Terminal it vanishes behind the window
        # border and cut the cards' right border.
        cols = max(80, cols - 2)
        with self.lock:
            rows = list(self.rows)
            current = self.current()
            items = self.items()
        self.sync_reply_target(current)
        counts = {k: sum(1 for r in rows if r["group"] == k) for k in ("needs", "working", "done")}
        usage = plan_usage() if self.on("plan_usage") else ""
        info = [
            # Version and, beside it, plan usage. The rest (model of new sessions,
            # count per state) already shows in the list and the groups.
            f"{BOLD}Claude Code{RESET} {DIM}v{self.version}{RESET}" + (f"   {usage}" if usage else ""),
            "",
            "",
        ]
        # No "waiting for you" button at the top (you asked to remove it); f2 still jumps.
        self.waiting_button = (0, 0, 0)
        # The alert goes away when you stay 3 s on the session (passing over it with the arrow does not count).
        here = current[1]["id"] if current and current[0] == "row" else None
        if here != getattr(self, "here_id", None):
            self.here_id, self.here_since = here, time.time()
        if here in self.alerts and self.on("alert_clear") and time.time() - self.here_since > 3:
            self.alerts.pop(here, None)
            self.save_config()
            self.follow(here)   # it goes back to its group; the selection goes along
        lines = [f" {ORANGE}{LOGO[i]}{RESET}  {info[i]}" for i in range(3)] + [""]
        # Buttons in the top right corner: switch the view and Refresh. The click lands
        # in self.view_button / self.button.
        # 1-column symbols: ☰ takes 2 in the terminal and pushed the screen down a line.
        view_label = ""   # no button to switch the view: there is only List + conversation
        used = 1 + text_width(LOGO[0]) + 2 + visible_width(info[0])
        help_w = text_width(HELP_LABEL) + 1
        feat_w = text_width(FEAT_LABEL) + 1
        start = max(used + 2 + help_w + feat_w, cols - text_width(RELOAD_LABEL) - 1 - text_width(view_label))
        self.help_button = (start - help_w + 1, start - 1)
        self.feat_button = (start - help_w - feat_w + 1, start - help_w - 1)
        self.view_button = (start + 1, start + text_width(view_label))
        reload_at = start + text_width(view_label) + 1
        self.button = (reload_at + 1, reload_at + text_width(RELOAD_LABEL))
        self.view_button = (0, -1)   # nothing to click
        lit = ESC + "48;2;215;119;87m" + ESC + "38;2;20;20;20m"   # lit button (panel open)
        help_style = lit if self.help else BAR
        lines[0] += (" " * (start - help_w - feat_w - used) + (lit if self.feat_open else BAR) + BOLD + FEAT_LABEL
                     + RESET + " " + help_style + BOLD + HELP_LABEL + RESET + " "
                     + BAR + BOLD + RELOAD_LABEL + RESET)
        if not self.loaded:
            lines.append(f"{DIM}  loading sessions…{RESET}")

        if self.view in ("kanban", "grid", "board", "focus"):
            bottom = self.bottom(cols)
            room = total - len(bottom) - len(lines)
            with self.lock:
                self.pane_hits = (None, [])   # without a side conversation, nothing in it is clickable
                self.pane_geo = None
                if self.view == "kanban" and self.peek:
                    # Split screen: Kanban on the left, the chosen card's conversation on the right.
                    left_w = max(64, cols * 55 // 100)
                    board_lines = self.kanban_lines(left_w, room, top_y=len(lines) + 1)
                    self.pane_at = (left_w + 2, len(lines) + 1)
                    pane = self.session_pane(self.kanban_selected(), cols - left_w - 1, room)
                    for i in range(room):
                        left = board_lines[i] if i < len(board_lines) else ""
                        right = pane[i] if i < len(pane) else ""
                        lines.append(fit_ansi(left, left_w) + " " + right)
                else:
                    board = {"kanban": self.kanban_lines, "grid": self.grid_lines, "board": self.board_lines,
                             "focus": self.focus_lines}[self.view]
                    lines += board(cols, room, top_y=len(lines) + 1)
            lines = lines[:total - len(bottom)]
            lines += [""] * (total - len(bottom) - len(lines))
            self.paint(lines, bottom)
            return

        if self.view == "split":
            # List + conversation: the main list on the left and the live conversation of the
            # chosen session on the right.
            bottom = self.bottom(cols)
            room = total - len(bottom) - len(lines)
            # The list only has name, PRs and time: it stays narrow and the conversation gets the rest.
            left_w = 60
            body, sel_line = self.list_body(items, current, left_w, compact=True)
            start = max(0, min(sel_line - room // 2, len(body) - room))
            body = body[start:start + room]
            # For the click: the screen line (from 1) of the list's 1st line, the offset and the width.
            self.list_click = (len(lines) + 1, start, left_w, list(self.list_where))
            with self.lock:
                row = current[1] if current and current[0] == "row" else None
                self.pane_at = (left_w + 2, len(lines) + 1)
                pane = self.session_pane(row, cols - left_w - 1, room)
            for i in range(room):
                left = body[i] if i < len(body) else ""
                right = pane[i] if i < len(pane) else ""
                lines.append(fit_ansi(left, left_w) + " " + right)
            self.paint(lines, bottom)
            return

        body, sel_line = self.list_body(items, current, cols)
        hdr = len(lines)
        sel_line += len(lines)
        lines += body
        self.list_click = (hdr + 1, 0, cols, list(self.list_where))

        bottom = self.bottom(cols)
        room = total - len(bottom)
        if len(lines) > room:
            start = max(0, min(sel_line - room // 2, len(lines) - room))
            lines = lines[:4] + lines[max(4, start + 4):max(4, start + 4) + room - 4]
            # Scrolled: the first 4 lines stay; the list continues from start + 4.
            if hdr <= 4:
                self.list_click = (5, max(4, start + 4) - hdr, cols, self.list_click[3])
        lines += [""] * (room - len(lines))
        self.paint(lines, bottom)

    def list_body(self, items, current, cols, compact=False):
        """List lines (group titles and sessions) and the position of the chosen one.
        compact drops model and effort, to fit beside the conversation."""
        lines, where = [], []   # where: the item of each line (None on blank lines), for the click
        name_w, model_w, effort_w, badge_w, age_w = (22, 0, 0, 8, 7) if compact else (22, 10, 7, 8, 7)
        summary_w = max(10, cols - 3 - name_w - 1 - model_w - 1 - effort_w - 1 - badge_w - 1 - age_w - 1)
        if compact:
            # Beside the conversation the summary, PRs and time go away; the status line takes their place.
            name_w, summary_w = 20, 0
        label_w = max([text_width(model_label(r)) for kind, r, _ in items if kind != "head"] or [1])
        sel_line = 0
        titles = dict(GROUPS, check=CHECK_TITLE)
        titles.update({key: key[len("custom:"):] for kind, key, _ in items if kind == "head" and key.startswith("custom:")})
        for index, (kind, value, group_rows) in enumerate(items):
            is_sel = current is not None and index == self.cursor
            if kind == "head":
                if lines and lines[-1] != "" and index > 0:
                    lines.append("")
                    where.append(None)
                arrow = " ▸" if value in self.collapsed else ""
                title = f"{titles[value]}{arrow}" + (f" {DIM}({len(group_rows)}){RESET}" if value in self.collapsed else "")
                if is_sel:
                    sel_line = len(lines)
                    lines.append(BAR + BOLD + fit(titles[value] + arrow, cols) + RESET)
                else:
                    lines.append(title)
                where.append(index)
                continue
            r = value
            badge, badge_len = self.badge(r)
            name = fit(r["name"], name_w)
            name = BOLD + name + RESET if is_sel else name
            meta = "" if compact else f"{DIM}{fit(short_model(r['model']), model_w)} {fit(r['effort'], effort_w)}{RESET} "
            if compact:
                line = f" {self.marker(r)} {name} " + (fit_ansi(status_short(r, model_w=label_w), max(1, cols - name_w - 4))
                                                       if self.on("list_columns") else "")
                if is_sel:
                    sel_line = len(lines)
                    line = BAR + line.replace(RESET, RESET + BAR) + " " * max(0, cols - visible_width(line)) + RESET
                lines.append(line)
                where.append(index)
                continue
            line = (
                f" {self.marker(r)} {name} {meta}"
                f"{fit(r['summary'], summary_w) + ' ' if summary_w else ''}{' ' * (badge_w - badge_len)}{badge} {fit(duration(r['elapsed']), age_w, 'right')}"
            )
            if is_sel:
                # Chosen line: gray strip across the full width, like the group title.
                # Each RESET in the middle restores the background so the strip has no holes.
                sel_line = len(lines)
                visible = visible_width(line)
                line = BAR + line.replace(RESET, RESET + BAR) + " " * max(0, cols - visible) + RESET
            lines.append(line)
            where.append(index)
        self.list_where = where
        return lines, sel_line

    def thumbs(self):
        """Pasted images (alt+v) still in the text: [(number, file)]."""
        images = getattr(self, "images", {})
        return [(n, images[n]) for n in re.findall(r"\[Imag(?:em|e) #(\d+)\]", self.prompt) if n in images
                and os.path.exists(images[n])]

    def tick_tooltip(self, frame):
        """Small box with the prompt of the ┤ under the mouse, to the left of the conversation bar."""
        hover = getattr(self, "tick_hover", None)
        row, ticks = getattr(self, "tick_hits", (None, []))
        if not hover or not row or row["id"] != hover[0]:
            return frame
        _, n, x, y = hover
        prompts = all_prompts(row.get("transcript"))
        # Nearby prompts fall on the same ┤ of the bar (long conversation, short bar): the box
        # shows all of that point, one under the other, not just the first.
        here = sorted({k for _, _, yy, k in ticks if yy == y and 1 <= k <= len(prompts)}) or (
            [n] if 1 <= n <= len(prompts) else [])
        if not here:
            return frame
        width = min(70, max(20, x - 6))
        each = 4 if len(here) == 1 else 2   # several: 2 lines each
        box = []
        for j, k in enumerate(here[:6]):
            lines = wrap(" ".join(prompt_text(prompts[k - 1]).split()), width - 4)
            body = lines[:each] or [""]
            if len(lines) > each:
                body[-1] = fit(body[-1], width - 5).rstrip() + "…"
            when = prompt_when(row.get("transcript"), k)
            title = f" prompt {k}{' · ' + when if when else ''} "
            corner = ("╭", "╮") if j == 0 else ("├", "┤")
            box.append(f"{corner[0]}{title}{'─' * max(0, width - 2 - len(title))}{corner[1]}")
            box += [f"│ {fit(l, width - 4)} │" for l in body]
        if len(here) > 6:
            box.append(f"│ {fit(f'… +{len(here) - 6} more prompts here', width - 4)} │")
        box.append(f"╰{'─' * (width - 2)}╯")
        left = max(0, x - 1 - width - 1)   # column (from 0) where the box starts
        top = max(0, min(y - 1 - len(box) // 2, len(frame) - len(box)))
        frame = list(frame)
        color = ESC + "48;2;30;34;44m" + ESC + "38;2;220;225;235m"
        for k, l in enumerate(box):
            i = top + k
            if i < len(frame):
                frame[i] = slice_ansi(frame[i], 0, left) + RESET + color + l + RESET + slice_ansi(frame[i], left + width, float("inf"))
        return frame

    def paint(self, lines, bottom):
        """Writes only the lines that changed since the last draw. This way the thumbnails
        (Sixel) of pasted images stay on screen: rewriting the line would erase the image."""
        frame = self.tick_tooltip(lines + bottom)
        self.queue_top = len(lines) + getattr(self, "queue_offset", 0)   # line (from 0) of the 1st queue line
        # The feature panel keys, at the screen line (from 1) where they ended up.
        self.feat_hits = [(self.queue_top + r + 1, a, b, k) for r, a, b, k in getattr(self, "feat_rel", [])] \
            if self.feat_open else []
        size = shutil.get_terminal_size((160, 45))
        last = getattr(self, "last_frame", None)
        out, changed = [], set()
        if last is None or getattr(self, "last_size", None) != size or len(last) != len(frame):
            out.append(ESC + "H" + "\n".join(l + ESC + "K" for l in frame) + ESC + "J")
            changed = set(range(len(frame)))
        else:
            for i, l in enumerate(frame):
                if l != last[i]:
                    # Erase the whole line first: writing over an emoji (2 columns) left a
                    # hole in Windows Terminal (the 🧠 and ⚡ vanished on a list line).
                    out.append(f"{ESC}{i + 1};1H{ESC}2K{l}")
                    changed.add(i)
        thumbs = self.thumbs()
        top = len(lines)
        key = (tuple(thumbs), top)
        if thumbs and (key != getattr(self, "thumb_key", None) or any(top <= i < top + THUMB_ROWS for i in changed)):
            x = 3
            for n, path in thumbs:
                data, w = sixel(path, THUMB_ROWS - 1)
                if data:
                    out.append(f"{ESC}{top + 1};{x}H" + data)
                    x += w + 2
        self.thumb_key = key if thumbs else None
        if out:
            # Half an emoji that slipped through becomes "?" instead of crashing the program.
            sys.stdout.write("".join(out).encode("utf-8", "replace").decode("utf-8"))
            sys.stdout.flush()
        self.last_frame, self.last_size = frame, size

    def bottom(self, cols):
        rows = self._bottom(cols)
        thumbs = self.thumbs()
        self.queue_offset = THUMB_ROWS if thumbs else 0   # the queue starts after the thumbnails
        if not thumbs:
            return rows
        # Free lines for the thumbnails and, under each one, its number.
        label, x = "  ", 3
        for n, path in thumbs:
            w = sixel(path, THUMB_ROWS - 1)[1] or 12
            cell = f"[Image #{n}]"
            label += " " * max(0, x - 1 - visible_width(label)) + DIM + cell + RESET
            x += w + 2
        return [""] * (THUMB_ROWS - 1) + [label] + rows

    def _bottom(self, cols):
        rule = DIM + "─" * cols + RESET
        if self.mode == "reply" and self.reply_to:
            label = f"→ {self.reply_to['name']}: "
        elif self.mode == "find":
            label = "find: "
        elif self.mode == "group" and self.reply_to:
            label = f"group for {self.reply_to['name']}: "
        elif self.mode == "rename" and self.reply_to:
            label = ("new group name: " if isinstance(self.reply_to, tuple)
                     else f"new name for {self.reply_to['name']}: ")
        else:
            label = ""
        raw = self.find if self.mode == "find" else self.prompt
        # What shows in the box and, for each screen letter, its position in the text: the cursor
        # and the click move over the real text. The break (ctrl+j, pasted enter) skips the line.
        text, back = raw, list(range(len(raw) + 1))
        pos = self.caret_pos() if self.mode != "find" else len(raw)
        cpos = back.index(pos) if pos in back else len(text)
        self.prompt_spans = []
        # White prompt arrow, as in the original.
        if text or label:
            # Long text wraps into lines that start where the text starts (after the "→ name: "),
            # not at the screen corner.
            indent = 2 + text_width(label)
            # Each line of the text wraps on its own; the "\n" stays at the end of its line (which is where the
            # cursor shows when on it) and on screen becomes a space.
            spans, s = [], 0
            for part in text.split("\n"):
                e = s + len(part)
                pieces = [(s + a, s + b) for a, b in wrap_spans(part, max(10, cols - indent - 1))]
                if e < len(text):
                    pieces[-1] = (pieces[-1][0], e + 1)
                spans += pieces
                s = e + 1
            # Up to 8 lines, always with the cursor's line in view.
            at = next((k for k, (a, b) in enumerate(spans) if a <= cpos < b), len(spans) - 1)
            first = max(0, min(at - 7, len(spans) - 8))
            prompt, top = [], 0
            if first:
                # Pasted text bigger than the box: the 1st line says who it goes to and how much is left
                # above (without it, it looked like the start of the text had vanished). It takes up one
                # of the 8 lines, so it shows 7 of the text, with the cursor's line in view.
                first = max(1, min(at - 6, len(spans) - 7))
                prompt.append(f"❯ {DIM}{label}↑ {first} more line{'s' if first > 1 else ''} above "
                              f"({len(text)} characters, all of it is sent){RESET}")
                top = 1
            spans = spans[first:first + 8 - top]
            for k, (a, b) in enumerate(spans):
                seg = text[a:b].replace("\n", " ")
                if a <= cpos < b:   # the cursor is the letter in reverse video
                    i = cpos - a
                    seg = seg[:i] + f"{ESC}7m{seg[i]}{RESET}" + seg[i + 1:]
                elif cpos == b and k == len(spans) - 1:
                    seg += f"{ESC}7m {RESET}"
                prompt.append((f"❯ {DIM}{label}{RESET}" if k == 0 and first == 0 else " " * indent) + seg)
                self.prompt_spans.append((k + top, indent + 1, a, b))
            self.prompt_back = back
        else:
            ghost = "describe a task for a new session"
            item = self.current()
            if self.view in ("kanban", "split") and self.compose_new:
                ghost = "new session: describe the task (esc cancels)"
            elif self.view in ("kanban", "split") and item and item[0] == "row":
                ghost = f"type to message {item[1]['name']} · ctrl+n new session"
            if self.view == "board":
                ghost = "new goal name" if self.bside else "write a task for this goal"
            prompt = [f"❯ {ESC}7m{ghost[0]}{RESET}{DIM}{ghost[1:]}{RESET}"]
        if self.feat_open:
            # Features panel: two columns of keys; click toggles (self.feat_hits).
            title = f"  {BOLD}Features{RESET} {DIM}· click to turn on/off · saved for next time · esc or the button closes{RESET}"
            out = [rule, *prompt, rule, title]
            ncol = 3 if cols >= 170 else 2
            col_w = (cols - 4) // ncol
            self.feat_rel = []   # (line within the footer, start column, end, key)
            for area in dict.fromkeys(f[3] for f in FEATURES):   # one area at a time, with its title
                feats = [f for f in FEATURES if f[3] == area]
                out.append("  " + ORANGE + BOLD + area.upper() + RESET)
                for start in range(0, len(feats), ncol):
                    line, x = "  ", 3
                    for key, name, what, _ in feats[start:start + ncol]:
                        on = self.on(key)
                        box = (GREEN + "● on " if on else DIM + "○ off") + RESET
                        cell = fit_ansi(f"{box} {BOLD if on else DIM}{name}{RESET} {DIM}{what}{RESET}", col_w - 2)
                        self.feat_rel.append((len(out), x, x + col_w - 3, key))
                        line += cell + " " * max(2, col_w - visible_width(cell))
                        x += col_w
                    out.append(line.rstrip())
            self.prompt_rel = 1
            return out
        if self.help:
            entries = HELP.get(self.view, []) + HELP["all"]
            half = (len(entries) + 1) // 2
            col_w = max(40, (cols - 4) // 2)
            key_w = 16
            cell = lambda e: (ORANGE + BOLD + fit(e[0], key_w) + RESET + fit(e[1], col_w - key_w - 1)) if e else ""
            hints = []
            for i in range(half):
                left = cell(entries[i])
                right = cell(entries[half + i]) if half + i < len(entries) else ""
                hints.append("  " + left + " " + right)
            title = f"  {BOLD}Shortcuts{RESET} {DIM}· ? or the button at the top closes{RESET}"
            self.prompt_rel = 1   # the 1st line of the box, counting from the start of the footer
            return [rule, *prompt, rule, title] + hints
        # The session mode (auto mode, plan mode…) sits to the left of the hint, as in Claude.
        row = self.reply_to if self.mode == "reply" else None
        item = self.current() if row is None and self.view in ("kanban", "split", "list", "focus") else None
        row = row if isinstance(row, dict) else item[1] if item and item[0] == "row" else None
        mode = self.mode_of(row) if row and row.get("alive") else ""
        if mode:
            mode += f" {DIM}(shift+tab to cycle) · {RESET}"
        tip = self.notice or self.hint()
        queued = self.queue_rows(row, cols)
        self.prompt_rel = len(queued) + 1
        return [*queued, rule, *prompt, rule, f"  {mode}{DIM}{fit(tip, cols - 2 - visible_width(mode))}{RESET}"]

    def queue_rows(self, row, cols):
        """The queue right above the box, as in Codex: one line per message, with the buttons
        ↳ steer (send now, mid-work), ✎ (edit) and ✕ (cancel)."""
        qedit = getattr(self, "qedit", None)
        if qedit:
            row = next((r for r in self.rows if r["id"] == qedit["id"]), row)
        items = self.waitlist.get(row["id"]) if row else None
        self.queue_marks = []   # (line within the queue, start, end, action, id, position)
        if not items:
            return []
        editing = getattr(self, "qediting", None)
        right = "↳ steer   ✎   ✕"
        out = []
        for i, m in enumerate(items[:5]):
            text = " ".join(prompt_text(m["text"]).split())
            body = fit(text, max(10, cols - text_width(right) - 10)).rstrip()
            mark = "✎" if editing and editing[1] is m else "⤷"
            left = f"  {mark} {body}"
            pad = " " * max(2, cols - text_width(left) - text_width(right) - 2)
            x = text_width(left) + len(pad) + 1   # column (from 1) where the "↳ steer" starts
            self.queue_marks += [(i, x, x + 6, "steer", row["id"], i), (i, x + 10, x + 10, "edit", row["id"], i),
                                 (i, x + 14, x + 14, "cancel", row["id"], i)]
            on = bool(qedit and qedit["id"] == row["id"] and qedit["sel"] == i)
            if on:
                line = left + pad + right
                out.append(ESC + "48;2;45;55;72m" + line + " " * max(0, cols - text_width(line)) + RESET)
            else:
                out.append(left + DIM + pad + right + RESET)
        if len(items) > 5:
            out.append(DIM + f"  … +{len(items) - 5} more in the queue · alt+↑ to see them" + RESET)
        return out

    def hint(self):
        item = self.current()
        if self.confirm is not None and self.confirm[0] in ("task", "goal"):
            what = "the task" if self.confirm[0] == "task" else "the goal and its tasks"
            return f"ctrl+x again deletes {what} (agents stay in the list) · any other key cancels"
        if self.confirm is not None:
            if self.confirm[0] == "head" and self.confirm[1].startswith("custom:"):
                return f"ctrl+x again to ungroup {self.confirm[1][7:]} (sessions are kept) · any other key to cancel"
            what = "all sessions in this group" if self.confirm[0] == "head" else self.confirm[1]["name"]
            return f"ctrl+x again to delete {what} · any other key to cancel"
        if self.mode == "group":
            order, _ = self.custom()
            known = f" · groups: {', '.join(order)}" if order else ""
            pick = "↑↓ to pick a group · " if order else ""
            return f"{pick}enter to save · empty removes from group · esc to cancel{known}"
        if self.mode == "rename":
            return "enter renames · esc cancels"
        if getattr(self, "qedit", None):
            return "queue · ↑↓ pick · enter edits · s sends now · shift+↑↓ moves · del cancels · ctrl+k clears all · esc closes"
        if self.mode == "reply" and getattr(self, "qediting", None):
            return "editing a queued message · enter or tab saves · esc keeps the old text"
        if self.mode == "reply":
            return "enter sends · tab queues (goes when it finishes) · shift+enter new line · ↑ last prompt · esc cancels"
        if self.mode == "find":
            return "type to filter · enter to keep · esc to clear"
        if self.prompt and self.view == "board":
            if self.bside:
                return "enter creates the goal · esc clears"
            return "enter creates the task (1st line = title) · shift+enter new line · esc clears"
        if self.prompt:
            return "enter to create · esc to clear · ctrl+f to find"
        if self.view == "board" and self.bside:
            return "↑↓ pick a goal · → go to tasks · type and enter creates a goal · ctrl+x deletes the goal · tab list"
        if self.view == "board":
            return ("enter launches / opens the agent · ctrl+a another agent · space replies · "
                    "shift+→ marks done · ← goals · ctrl+x deletes · tab list")
        if self.view == "split" and not (item and item[0] == "head"):
            return "↑↓ pick · type to reply · enter opens · ctrl+↑↓ / pgup pgdn scroll the conversation · ctrl+n new session · ? shortcuts"
        if self.view == "kanban":
            return ("←→↑↓ pick · type to reply · enter opens · ctrl+o side conversation · ctrl+n new session · "
                    "shift+←/→ change column · ? shortcuts")
        if self.view == "grid":
            return "↑↓ to pick · enter to open · space to reply · ctrl+g to group · ctrl+x to delete · tab list view"
        if item and item[0] == "head":
            verb = "expand" if item[1] in self.collapsed else "collapse"
            drop = "ctrl+x to ungroup" if item[1].startswith("custom:") else "ctrl+x to delete all"
            return f"enter to {verb} · {drop} · ? for shortcuts"
        return "enter to open · space to reply · ctrl+g to group · ctrl+x to delete · ? for shortcuts"

    # ------------------------------------------------ actions

    def run_claude(self, args, cwd=None):
        ran = subprocess.run(["claude", *args], capture_output=True, text=True, encoding="utf-8", cwd=cwd)
        if ran.returncode == 0:
            lines = ran.stdout.strip().splitlines()
            return True, (lines[0] if lines else "")
        lines = (ran.stderr or ran.stdout).strip().splitlines()
        return False, (lines[0] if lines else f"exit {ran.returncode}")

    def new_session_dir(self):
        # Claude does not create a background session in the user's folder ("Workspace
        # not trusted"). Opened from there, the new session goes to the chosen
        # session's folder, which Claude already trusts.
        home = os.path.normcase(os.path.expanduser("~"))
        usable = lambda path: bool(path) and os.path.normcase(path) != home and os.path.isdir(path)
        here = os.getcwd()
        if usable(here):
            return here
        item = self.current()
        if item and item[0] == "row" and usable(item[1].get("cwd")):
            return item[1]["cwd"]
        return next((r["cwd"] for r in self.rows if usable(r.get("cwd"))), here)

    def background(self, fn, *args):
        def run():
            try:
                fn(*args)
            except Exception:
                # An error in a background task goes to error.log, not over the screen drawing.
                import traceback
                try:
                    with open(os.path.join(DATA_DIR, "error.log"), "a", encoding="utf-8") as f:
                        f.write(time.strftime("%d/%m %H:%M:%S ") + traceback.format_exc() + "\n")
                except OSError:
                    pass
                self.notice, self.notice_until = f"error in {getattr(fn, '__name__', 'task')} · see error.log", time.time() + 6
        threading.Thread(target=run, daemon=True).start()

    def new_session(self, task):
        where = self.new_session_dir()
        self.notice = f"starting a new session in {home_path(where)}…"
        ok, msg = self.run_claude(["--bg", task], cwd=where)
        self.notice = f"new session · {msg}" if ok else f"error: {msg}"
        self.refresh()

    def reply(self, row, text):
        # Live session: attach to it underneath (claude attach) and type, as claude
        # agents does. Working or not, the message arrives right away and the model
        # decides when to read it. A live session is never stopped to reopen: if typing
        # failed, the notice stays. Stopped (no process): resume with the message.
        # The message already shows at the end of the conversation, with "sending…" and then "✓".
        if text.strip().lower() in ("/exit", "/quit"):
            # On the hidden connection, /exit only removes it from the session (back to the claude
            # agents list) and the session keeps running. Here it does what is expected: ends the session.
            with _attached_lock:
                old = _attached.pop(row["id"], None)
            if old:
                old.close()
            # Stays in the conversation, like messages: "/exit" and what happened.
            done = {"text": text.strip(), "state": "sending", "t": time.time()}
            with self.lock:
                self.outbox.setdefault(row["id"], []).append(done)
            if not row["alive"]:
                done["state"] = "closed"
                self.notice, self.notice_until = f"{row['name']} is already closed", time.time() + 5
                return
            self.notice, self.notice_until = f"closing {row['name']}…", time.time() + 10
            # First /exit goes into the real session (recorded in its conversation). There it
            # only drops the hidden connection; the stop right after is what closes the session.
            _safe(attach_send, row, text.strip())
            with _attached_lock:
                old = _attached.pop(row["id"], None)
            if old:
                old.close()
            ok, msg = self.run_claude(["stop", row["id"]])
            done["state"], done["why"] = ("closed", "") if ok else ("error", msg)
            self.notice, self.notice_until = (f"{row['name']} closed · your next message continues the conversation"
                                              if ok else f"could not close {row['name']}: {msg}"), time.time() + 8
            self.refresh()
            return
        sent_msg = {"text": text, "state": "sending", "t": time.time()}
        with self.lock:
            box = self.outbox.setdefault(row["id"], [])
            box[:] = [m for m in box if m["state"] != "closed"] + [sent_msg]   # reopened: the "/exit" goes away
        try:
            if row["alive"]:
                sent, msg = attach_send(row, text)
                if sent is False and "question or permission" not in msg:
                    # The session may have restarted right then (Claude updates itself and
                    # reopens sessions): new connection, wait for it to come back and send again.
                    sent_msg["state"] = "sending"
                    with _attached_lock:
                        old = _attached.pop(row["id"], None)
                    if old:
                        old.close()
                    for _ in range(30):
                        if any(s.get("id") == row["id"] and s.get("pid") for s in agents_json()):
                            break
                        time.sleep(1)
                    sent, msg = attach_send(row, text)
                sent_msg["state"] = "ok" if sent else "unsure" if sent is None else "error"
                sent_msg["why"] = msg
                if sent and text.startswith("/"):
                    # The command may open a menu in the session (/model, /config…): it shows up here.
                    self.panel = {"id": row["id"], "t": time.time(), "seen": False}
                if not sent:
                    self.notice, self.notice_until = f"{row['name']}: {msg}", time.time() + 15
                self.refresh()
                return
            self.notice, self.notice_until = f"sending to {row['name']}… (it reopens, takes ~15 s)", time.time() + 120
            # Closed: claude attach reopens the session with the same id and the same conversation. The
            # --resume is only a fallback: on an old session it opened the wrong conversation or a copy.
            sent, msg = attach_send(row, text)
            if sent is not False:
                sent_msg["state"], sent_msg["why"] = ("ok" if sent else "unsure"), msg
                self.notice, self.notice_until = f"sent to {row['name']} ✓", time.time() + 15
                self.refresh()
                return
            with _attached_lock:
                old = _attached.pop(row["id"], None)
            if old:
                old.close()
            ok, msg = self.resume_with(row, text)
            sent_msg["state"], sent_msg["why"] = ("ok" if ok else "error"), msg
            self.notice, self.notice_until = (f"sent to {row['name']} ✓" if ok else f"error: {msg}"), time.time() + 15
            self.refresh()
        except Exception as exc:   # the notice and the message never stay stuck on "sending…"
            sent_msg["state"], sent_msg["why"] = "error", str(exc)
            self.notice, self.notice_until = f"error sending to {row['name']}: {exc}", time.time() + 15

    def resume_with(self, row, text):
        """Stopped session: resume with the message, under the same id. If --resume creates a
        copy with another id (the session still counted as running), delete the copy and try again."""
        ok, msg = False, "could not reopen the session (each try opened a copy with another id)"
        for _ in range(3):
            if any(s.get("id") == row["id"] and s.get("pid") for s in agents_json()):
                self.run_claude(["stop", row["id"]])
                for _ in range(30):
                    if not any(s.get("id") == row["id"] and s.get("pid") for s in agents_json()):
                        break
                    time.sleep(1)
                time.sleep(2)
            # The real conversation is the file the session writes (on an old session, the sessionId
            # in state.json points to another, empty one).
            convo = os.path.basename(row.get("transcript") or "")[:-6] if (row.get("transcript") or "").endswith(".jsonl") else ""
            ran = subprocess.run(["claude", "--bg", "--resume", convo or row["sessionId"], text], capture_output=True,
                                 text=True, encoding="utf-8", cwd=row.get("cwd") or None, timeout=120)
            out = (ran.stdout or "") + (ran.stderr or "")
            got = re.search(r"backgrounded · (\w+)", out)
            if ran.returncode != 0 or not got:
                return False, (out.strip().splitlines() or [f"exit {ran.returncode}"])[0]
            if got.group(1) == row["id"]:
                return True, ""
            self.run_claude(["stop", got.group(1)])
            time.sleep(2)
            self.run_claude(["rm", got.group(1)])
        return ok, msg

    def delete(self, rows):
        for row in rows:
            self.notice = f"deleting {row['name']}…"
            if row["alive"]:
                self.run_claude(["stop", row["id"]])
            ok, msg = self.run_claude(["rm", row["id"]])
            if not ok:
                self.notice = f"error: {msg}"
                break
        else:
            self.notice = "deleted"
        self.refresh()

    def attach(self, row):
        # Going: our screen is also drawn on the terminal's alternate screen. When
        # leaving the session, Claude takes a while closing and shows that alternate
        # screen: so the list appears, not a black screen.
        with _attached_lock:
            if row["id"] in _attached:
                _attached.pop(row["id"]).close()
        self.console(False)
        self.notice, self.notice_until = f"coming back from {row['name']}…", 0
        self.last_frame = None
        sys.stdout.write(ESC + "?1049l")
        self.draw()
        # Cursor at the top: the text Claude writes when leaving the session lands
        # on top of the list (and disappears on redraw), instead of at the bottom of the screen, where it
        # pushed everything up.
        sys.stdout.write(ESC + "H" + ESC + "?25h")
        sys.stdout.flush()
        # In the session's folder: the ← inside it opens the original claude agents, and in
        # the user's folder it stopped asking whether the folder is trusted.
        where = row.get("cwd") if row.get("cwd") and os.path.isdir(row["cwd"]) else None
        attached = subprocess.Popen(["claude", "attach", row["id"]], cwd=where)
        # The ← inside the session opens the original claude agents as a child of attach.
        # When that child appears, we close both and return to this screen.
        while attached.poll() is None:
            if any(name.lower() == "claude.exe" for name in descendants(attached.pid).values()):
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(attached.pid)], capture_output=True)
                attached.wait()
                sys.stdout.write(ESC + "?1049l")
                break
            time.sleep(0.15)
        # Back: turn off the modes the session left on the terminal and draw right away
        # with the data we already have; the list updates behind. Stays on the alternate
        # screen, which already shows the list: switching screens flashed black.
        sys.stdout.write(TERMINAL_RESET + ESC + "?25l")
        sys.stdout.flush()
        self.last_frame = None
        self.console(True)
        self.notice = ""
        self.mark_seen(row["id"])
        self.draw()
        self.background(self.refresh)

    # ------------------------------------------------ keyboard

    def jump_group(self, step):
        items = self.items()
        heads = [i for i, it in enumerate(items) if it[0] == "head"]
        if not heads:
            return
        if step < 0:
            before = [i for i in heads if i < self.cursor]
            self.cursor = before[-1] if before else heads[0]
        else:
            after = [i for i in heads if i > self.cursor]
            self.cursor = after[0] if after else heads[-1]

    def switch_view(self, item=None):
        # Tab goes List → Kanban → List + conversation → List.
        self.view = {"list": "kanban", "kanban": "split"}.get(self.view, "list")
        self.drag = None
        self.save_config()
        if item and item[0] == "row":
            self.follow(item[1]["id"])

    def mouse(self, kind, x, y):
        """Mouse: ('down'|'up'|'dbl', column, row), counting from 1.
        In Kanban: click selects the card, drag and drop onto another column moves it,
        double click opens the session."""
        if kind == "move":
            # Mouse on a ┤ of the conversation bar: shows its prompt in a small box beside it.
            row, ticks = getattr(self, "tick_hits", (None, []))
            hit = next(((n, x0, yy) for x0, x1, yy, n in ticks if yy == y and x0 <= x <= x1), None) if row else None
            hover = (row["id"], *hit) if hit else None
            if hover != getattr(self, "tick_hover", None):
                self.tick_hover = hover
                return True
        if kind == "down" and self.prompt and self.mode != "find":
            # Click on the text you are writing: the cursor goes to the clicked letter.
            base = getattr(self, "queue_top", -99) + getattr(self, "prompt_rel", 1)
            for k, x0, a, b in getattr(self, "prompt_spans", []):
                if y == base + k + 1:
                    back = getattr(self, "prompt_back", [])
                    i = min(b, a + max(0, x - x0))
                    if i < len(back):
                        p = back[i]
                        self.caret = None if p >= len(self.prompt) else p
                    return True
        if kind == "down":
            # Queue buttons, above the input box: ↳ steer, ✎ and ✕.
            for i, x0, x1, action, sid, idx in getattr(self, "queue_marks", []):
                if y == getattr(self, "queue_top", -9) + i + 1 and x0 <= x <= x1:
                    self.queue_action(action, sid, idx)
                    return True
        if kind in ("down", "dbl") and self.view in ("list", "split") and getattr(self, "list_click", None):
            # Click on a session (or a group title) in the list selects; double click opens.
            y0, start, width, where = self.list_click
            at = start + (y - y0)
            if x <= width and y >= y0 and 0 <= at < len(where) and where[at] is not None:
                self.cursor = where[at]
                self.qedit = None
                item = self.current()
                if kind == "dbl" and item and item[0] == "row" and not self.prompt:
                    self.attach(item[1])
                elif kind == "dbl" and item and item[0] == "head":
                    self.collapsed ^= {item[1]}
                return True
        if self.view in ("split", "kanban") and kind in ("down", "move", "up"):
            done = self.select_mouse(kind, x, y) if self.on("drag_copy") else None
            if done is not None:
                return done
        if kind == "move" and self.view != "kanban":
            return None
        if kind == "down" and y == self.waiting_button[0] and self.waiting_button[1] <= x <= self.waiting_button[2]:
            self.jump_waiting()
            return True
        if kind == "down" and y == 1 and self.button[0] <= x <= self.button[1]:
            self.reload = True
            return False
        if kind == "down" and y == 1 and self.help_button[0] <= x <= self.help_button[1]:
            self.help = not self.help
            return True
        if kind == "down" and y == 1 and self.view_button[0] <= x <= self.view_button[1]:
            self.switch_view(self.current())
            return True
        if kind in ("down", "dbl") and self.view in ("split", "kanban"):
            # Conversation beside: ☰ index opens / closes the list; clicking a prompt in it,
            # or a ┤ on the scroll bar, scrolls the conversation up to it.
            row, hits = getattr(self, "pane_hits", (None, []))
            hit = next((h for h in hits if h[0] <= x <= h[1] and h[2] == y), None)
            try:   # clicks.log: where the click was and the buttons on that line (to find a lost click)
                if not self.on("click_log"):
                    raise OSError   # turned off in the features panel
                log_path = os.path.join(DATA_DIR, "clicks.log")
                if os.path.exists(log_path) and os.path.getsize(log_path) > 512 * 1024:
                    os.replace(log_path, log_path + ".1")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} x={x} y={y} hit={hit[3] if hit else None} "
                            f"line={[(h[0], h[1], h[3]) for h in hits if h[2] == y][:8]}\n")
            except OSError:
                pass
            if hit and row:
                if hit[3] == "open":
                    self.open_file(hit[4])
                elif hit[3] == "copy":
                    text = hit[4]
                    ok = copy_text(text)
                    short = " ".join(text.split())
                    self.notice, self.notice_until = ((f"copied ✓ {fit(short, 60).rstrip()}" if ok
                                                       else "could not copy"), time.time() + 4)
                elif hit[3] == "copyfile":
                    # Windows "Copy" takes ~3 s: runs in the background, and the notice says when it is ready.
                    path = hit[4]
                    self.notice, self.notice_until = f"copying {os.path.basename(path)}…", time.time() + 6

                    def run():
                        ok = copy_file(path)
                        self.file_copied = (path, time.time())
                        self.notice, self.notice_until = ((f"file copied ✓ {os.path.basename(path)} · ctrl+v in Teams, "
                                                           "WhatsApp or e-mail attaches it") if ok
                                                          else "could not copy the file · use ⌕ in folder"), time.time() + 8
                    self.background(run)
                elif hit[3] == "showfile":
                    # Explorer with the file selected (the /select, goes along in the same argument).
                    subprocess.Popen(f'explorer /select,"{os.path.normpath(hit[4])}"', stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                     creationflags=0x08000000)
                    self.notice, self.notice_until = (f"{os.path.basename(hit[4])} selected in the folder · drag it "
                                                      "into the chat, or ctrl+c there and ctrl+v in the chat"), time.time() + 8
                elif hit[3] == "index":
                    self.index_open, self.index_sel = not self.index_open, None
                    self.arts_open = None
                elif hit[3] == "arts":
                    self.arts_open = None if getattr(self, "arts_open", None) == row["id"] else row["id"]
                    self.index_open = False
                elif self.hits_of(row):
                    self.index_sel = hit[4]
                    self.go_hit(row)
                else:
                    self.index_sel = hit[4]
                    self.index_go(row)
                return True
        if self.view == "board":
            side = next((h for h in self.shits if h[0] <= x <= h[1] and h[2] <= y <= h[3]), None)
            if side and kind in ("down", "dbl"):
                self.bgoal, self.bside, self.bcol, self.brow = side[4], True, 0, 0
                return True
            hit = next((h for h in self.bhits if h[0] <= x <= h[1] and h[2] <= y <= h[3]), None)
            if hit and kind in ("down", "dbl"):
                self.bside, self.bcol, self.brow = False, hit[4], hit[5]
                if kind == "dbl":
                    return self.key("\r")
            return True
        if self.view == "grid":
            # Panels: click selects (in the left list or in the panel), double click opens.
            hit = next((h for h in self.ghits if h[0] <= x <= h[1] and h[2] <= y <= h[3]), None)
            if hit and kind in ("down", "dbl"):
                self.gsel = hit[4]
                if kind == "dbl":
                    row = self.grid_selected()
                    if row:
                        self.attach(row)
            return True
        if self.view != "kanban":
            return True
        hit = next((h for h in self.hits if h[0] <= x <= h[1] and h[2] <= y <= h[3]), None)
        if kind == "move":
            # Hovering over the card shows the pop-up; leaving it hides it.
            if self.drag:
                return True
            if hit:
                columns = self.kanban_columns()
                rows = columns[hit[4]][2] if hit[4] < len(columns) else []
                row = rows[hit[5]] if hit[5] < len(rows) else None
                new = (row["id"], hit[:4]) if row else None
            else:
                new = None
            changed = new != self.hover
            self.hover = new
            return changed
        if kind == "down":
            if hit:
                self.kcol, self.krow = hit[4], hit[5]
                row = self.kanban_selected()
                self.drag = (hit[4], hit[5], row["id"]) if row else None
            else:
                column = next((c for c in getattr(self, "columns_at", []) if c[0] <= x <= c[1]), None)
                if column:
                    self.kcol, self.krow = column[2], 0
            return True
        if kind == "up":
            drag, self.drag = self.drag, None
            column = next((c for c in getattr(self, "columns_at", []) if c[0] <= x <= c[1]), None)
            if drag and column and column[2] != drag[0]:
                row = next((r for r in self.kanban_columns()[drag[0]][2] if r["id"] == drag[2]), None)
                self.move_card(row, column[2])
            return True
        if kind == "dbl" and hit:
            self.drag = None
            self.kcol, self.krow = hit[4], hit[5]
            row = self.kanban_selected()
            if row:
                self.attach(row)
        return True

    def key(self, ch):
        if isinstance(ch, tuple) and ch[0] == "move":
            # Mouse moving: only updates the pop-up; does not close shortcuts, notices or the pending ctrl+x.
            self.mouse(*ch)
            return True
        if ch == "\x1b" and getattr(self, "arts_open", None):   # esc closes the artifacts list
            self.arts_open = None
            return True
        if isinstance(ch, tuple) and ch[0] == "down" and ch[2] == 1 and self.feat_button[0] <= ch[1] <= self.feat_button[1]:
            self.feat_open, self.help = not self.feat_open, False   # ⊞ Features button opens/closes the panel
            return True
        if self.feat_open:
            if ch == "\x1b":
                self.feat_open = False
                return True
            if isinstance(ch, tuple) and ch[0] == "down":
                hit = next((h for h in self.feat_hits if h[0] == ch[2] and h[1] <= ch[1] <= h[2]), None)
                if hit:
                    self.features[hit[3]] = not self.on(hit[3])
                    if self.features[hit[3]]:
                        self.features.pop(hit[3])   # on is the default: no need to store it
                    self.save_config()
                    name = next(f[1] for f in FEATURES if f[0] == hit[3])
                    self.notice, self.notice_until = f"{name}: {'on' if self.on(hit[3]) else 'off'}", time.time() + 3
                    return True
        item = self.current()
        if isinstance(ch, tuple) and (ch[0] in ("move", "up") or (
                ch[0] == "down" and ch[2] == 1 and self.help_button[0] <= ch[1] <= self.help_button[1])):
            # Mouse moving or releasing the button is not a key: does not close shortcuts, does not
            # cancel what is in progress. Clicking Shortcuts opens and closes it.
            if ch[0] == "down":
                self.help = not self.help
                return True
            # Mouse moving with no change returns "skip": does not redraw and, more importantly,
            # is not the False that closes the program.
            return self.mouse(*ch) or "skip"
        if ch not in ("\x1b", "\x03") and not (isinstance(ch, tuple) and ch[0] in ("move", "up")):
            self.quitting = 0   # any other key (or click) gives up quitting
            self.stopping = (None, 0)   # and stopping the session
            if not isinstance(ch, tuple):
                self.sel = None   # a key clears the selection mark (the text was already copied)
        if getattr(self, "moving", None):
            # Moving a whole group: ↑↓ (or ←→ in Kanban) change its place; enter, esc or ctrl+g finish.
            if ch in ("\x00", "\xe0") and self.queue and self.queue[0] in ("H", "P", "K", "M"):
                code = self.getch()
                self.move_named(self.moving, -1 if code in ("H", "K") else 1)
                return True
            name, self.moving = self.moving, None
            self.notice, self.notice_until = f"group {name} moved", time.time() + 3
            if ch in ("\r", "\x1b", "\x07"):
                return True
        if ch != "\x18" or (self.confirm is not None and self.confirm[0] in ("task", "goal") and self.view != "board"):
            was_confirming = self.confirm is not None
            self.confirm = None
            if was_confirming and ch not in ("\x00", "\xe0"):
                return True
        if ch not in ("?",):
            self.help = False
        if time.time() >= self.notice_until:  # send notice stays on screen
            self.notice = ""

        if isinstance(ch, tuple):
            done = self.mouse(*ch)
            return "skip" if done is None else done
        if ch == "F2":
            self.jump_waiting()
            return True
        if ch == "S-TAB":
            # Writing to a session: switches its mode; otherwise, the chosen session's.
            target = self.reply_to if self.mode == "reply" and isinstance(self.reply_to, dict) \
                else item[1] if item and item[0] == "row" else None
            if target and target.get("alive"):
                self.background(self.switch_mode, target)
            return True
        # alt+↑: opens the queue to edit, reorder or cancel (like Codex).
        if ch == "\x00" and self.queue and self.queue[0] == ALT_ARROWS["H"] and not self.prompt.strip() \
                and not getattr(self, "qedit", None):
            row = self.reply_to if self.mode == "reply" and isinstance(self.reply_to, dict) \
                else item[1] if item and item[0] == "row" else None
            if row and self.waitlist.get(row["id"]):
                self.getch()
                self.mode, self.reply_to, self.prompt = "task", None, ""
                self.qedit = {"id": row["id"], "sel": len(self.waitlist[row["id"]]) - 1}
                return True
        if getattr(self, "qedit", None) and not isinstance(ch, tuple):   # queue open: the keys are its own
            return self.queue_key(ch)
        # Writing: ←/→, home/end and del move and delete inside the text (the cursor).
        if not self.prompt:
            self.caret = None
        typing = self.mode in ("reply", "group", "rename") or (self.mode == "task" and self.prompt)
        if typing and ch == "\xe0" and self.queue and self.queue[0] in ("K", "M", "G", "O", "S", "s", "t"):
            self.caret_key(self.getch())
            return True
        panel = getattr(self, "panel", None)
        if panel and item and item[0] == "row" and item[1]["id"] == panel["id"] and self.mode == "task" and not self.prompt:
            # Menu opened by a / command in the session: arrows, Enter, Esc and letters go to it.
            conn = _attached.get(panel["id"])
            if conn and not conn.dead.is_set() and conn.dialog():
                seq = None
                if ch in ("\x00", "\xe0") and self.queue and self.queue[0] in ("H", "P", "K", "M"):
                    seq = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D"}[self.getch()]
                elif isinstance(ch, str) and len(ch) == 1 and (ch in ("\r", "\x1b", "\t", "\b") or ch.isprintable()):
                    seq = "\x7f" if ch == "\b" else ch
                if seq is not None:
                    self.background(conn.press, seq)
                    return True
        if (isinstance(ch, str) and len(ch) == 1 and ch.isdigit() and self.mode == "task" and not self.prompt
                and time.time() >= getattr(self, "paste_until", 0)   # pasted text that starts with a number
                and item and item[0] == "row"):
            # Question or permission open in the session: the number picks the option there.
            conn = _attached.get(item[1]["id"])
            if conn and not conn.dead.is_set() and conn.dialog():
                self.background(self.press_option, conn, ch, item[1])
                return True
        if ch == "F5":   # updates the program (picks up the new code); the top button does the same
            self.reload = True
            return False
        if ch == "\x12" and self.mode == "task" and not self.prompt and item:
            # Ctrl+R, as in claude agents: renames the chosen session or, on the title
            # of one of your groups, the group.
            if item[0] == "head" and item[1].startswith("custom:"):
                self.mode, self.reply_to, self.prompt = "rename", ("group", item[1][7:]), item[1][7:]
            elif item[0] == "row":
                self.mode, self.reply_to, self.prompt = "rename", item[1], item[1]["name"]
            return True
        if ch in ("\x13", "\t") and not (self.mode in ("reply", "find", "group", "rename") or self.prompt):
            return True   # tab used to switch the view; now there is only List + conversation

        found = (self.hits_of(item[1]) if self.view in ("kanban", "split") and item and item[0] == "row"
                 and self.mode in ("find", "task") and not self.prompt else [])
        if found:
            # Search results in place of the conversation: ctrl+↑↓ picks, enter goes there.
            if ch in ("\x00", "\xe0") and self.queue and self.queue[0] in ("\x8d", "\x91", "W", "w"):
                step = -1 if self.getch() in ("\x8d", "W") else 1
                numbers = [n for n, _, _ in found]
                at = numbers.index(self.index_sel) if self.index_sel in numbers else len(numbers) - 1
                self.index_sel = numbers[max(0, min(len(numbers) - 1, at + step))]
                return True
            if ch == "\r":
                self.go_hit(item[1])
                return True
        if ch == "\xe0" and self.queue and self.queue[0] in ("W", "w"):
            code = self.getch()
            if self.view == "split" or (self.view == "kanban" and self.popup_target()):
                self.pscroll = max(0, self.pscroll + (3 if code == "W" else -3))
                return True
            self.queue.insert(0, "H" if code == "W" else "P")

        if self.view in ("kanban", "split") and (self.index_open or ch == "\x15"):
            # ctrl+u: index of your prompts; ↑↓ / PgUp PgDn pick, enter goes, esc closes.
            target = self.popup_target() if self.view == "kanban" else None
            row = target[0] if target else (item[1] if item and item[0] == "row" else None)
            if ch == "\x15":
                self.index_open, self.index_sel = not self.index_open, None
                return True
            if ch in ("\x00", "\xe0") and self.queue and self.queue[0] in ("H", "P", "I", "Q"):
                step = {"H": -1, "P": 1, "I": -10, "Q": 10}[self.getch()]
                self.index_sel = max(1, min(getattr(self, "index_total", 1), (self.index_sel or 1) + step))
                return True
            if ch == "\r":
                if row:
                    self.index_go(row)
                return True
            if ch == "\x1b":
                self.index_open = False
                return True

        menu = self.cmd_menu()
        if menu:
            if ch in ("\x00", "\xe0") and self.queue and self.queue[0] in ("H", "P"):
                self.cmd_sel += -1 if self.getch() == "H" else 1
                return True
            exact = self.prompt.strip()[1:] in {n for n, _ in menu}
            if ch == "\t" or (ch == "\r" and not exact):
                self.prompt = "/" + menu[self.cmd_sel % len(menu)][0] + " "
                return True

        # Queue, like Codex: tab holds the message for when the session finishes its turn.
        if ch == "\t" and self.mode == "reply" and isinstance(self.reply_to, dict) and self.prompt.strip():
            self.sync_reply_target()
            row = self.reply_to
            if not self.save_queued_edit():
                self.enqueue(row, self.with_images(self.prompt.strip()))
            self.prompt, self.mode, self.reply_to = "", "task", None
            return True
        if ch == "\xe0" and self.queue and self.queue[0] == "S":   # Delete outside the queue: nothing
            self.getch()
            return True

        if ch == "\x00" and self.queue and self.queue[0] == "V":
            self.getch()
            self.paste_image(item)
            return True

        if (ch in ("\x00", "\xe0") and self.queue and self.queue[0] in ("I", "Q", "\x8d", "\x91")
                and (self.view == "split" or (self.view == "kanban" and self.popup_target()))):
            # Scroll the conversation beside, even while you type: ctrl+↑/↓ 3 by 3
            # lines, PgUp/PgDn half a screen.
            code = self.getch()
            page = max(3, getattr(self, "pane_area", 20) // 2)
            step = {"I": page, "Q": -page, "\x8d": 3, "\x91": -3}[code]
            self.pscroll = max(0, self.pscroll + step)
            return True

        if self.view == "focus" and ch in ("\x00", "\xe0") and not (self.mode in ("reply", "find", "group", "rename") or self.prompt):
            code = self.getch()
            if code in ("K", "H"):
                self.focus_step(-1)
            elif code in ("M", "P"):
                self.focus_step(1)
            return True

        if self.view == "board" and not (self.mode in ("reply", "find", "group", "rename") or self.prompt):
            task = item[1] if item else None
            if ch in ("\x00", "\xe0"):
                code = self.getch()
                if self.bside:
                    if code in ("H", "P"):
                        self.bgoal += -1 if code == "H" else 1
                        self.bcol, self.brow = 0, 0
                    elif code == "M":
                        self.bside = False
                    return True
                if code == "H":
                    self.brow -= 1
                elif code == "P":
                    self.brow += 1
                elif code == "K":
                    if self.bcol == 0:
                        self.bside = True
                    self.bcol, self.brow = max(0, self.bcol - 1), 0
                elif code == "M":
                    self.bcol, self.brow = self.bcol + 1, 0
                elif code == "s":
                    self.move_task(task, self.bcol - 1)
                elif code == "t":
                    self.move_task(task, self.bcol + 1)
                return True
            if ch == "\x18" and self.bside:
                goal = self.goal_selected()
                if not goal:
                    return True
                if self.confirm is not None and self.confirm[0] == "goal" and self.confirm[1]["id"] == goal["id"]:
                    self.confirm = None
                    goals, tasks = self.board_data()
                    self.save_board([g for g in goals if g["id"] != goal["id"]],
                                    [t for t in tasks if t.get("goal") != goal["id"]])
                    self.bgoal = 0
                    self.notice = f"goal {goal['title']} deleted (agents stay in the list)"
                else:
                    self.confirm = ("goal", goal, None)
                return True
            if ch == "\r" and task:
                agents = self.task_agents(task)
                if agents:
                    self.attach(agents[-1])
                else:
                    self.background(self.launch_agent, task)
                return True
            if ch == "\x01" and task:   # Ctrl+A: one more agent on the same task
                self.background(self.launch_agent, task)
                return True
            if ch == " " and task:
                agents = self.task_agents(task)
                if agents:
                    self.mode, self.reply_to = "reply", agents[-1]
                else:
                    self.notice = "this task has no agent yet · enter launches one"
                return True
            if ch == "\x18" and task:
                if self.confirm is not None and self.confirm[0] == "task" and self.confirm[1]["id"] == task["id"]:
                    self.confirm = None
                    goals, tasks = self.board_data()
                    self.save_board(goals, [t for t in tasks if t["id"] != task["id"]])
                    self.notice = f"task {task['title']} deleted (agents stay in the list)"
                else:
                    self.confirm = item
                return True
            if ch in ("\x07", "\x10", "\x14", "\r", " ", "\x18"):
                return True

        if self.view == "grid" and ch in ("\x00", "\xe0") and not (self.mode in ("reply", "find", "group", "rename") or self.prompt):
            code = self.getch()
            step = {"H": -1, "P": 1, "K": -1, "M": 1, "I": -4, "Q": 4}.get(code, 0)
            self.gsel += step
            return True

        if self.view == "kanban" and ch in ("\x00", "\xe0") and not (self.mode in ("reply", "find", "group", "rename") or self.prompt):
            code = self.getch()
            row = item[1] if item else None
            self.hover = None   # arrows rule: the pop-up follows the chosen card, not the mouse
            self.pscroll = 0
            if code == "H":
                self.krow -= 1
            elif code == "P":
                self.krow += 1
            elif code in ("I", "Q"):
                self.krow += -5 if code == "I" else 5
            elif code == "K":
                self.kcol, self.krow = self.kcol - 1, 0
            elif code == "M":
                self.kcol, self.krow = self.kcol + 1, 0
            elif code == "s":   # Shift+← moves the card to the left column
                self.move_card(row, self.kcol - 1)
            elif code == "t":   # Shift+→ moves the card to the right column
                self.move_card(row, self.kcol + 1)
            elif code in ("U", "D"):   # Shift+↑↓ moves the group to another column position
                self.move_group(item, -1 if code == "U" else 1)
            return True

        if ch in ("\x00", "\xe0"):
            code = self.getch()
            moves = {"H": -1, "P": 1, "I": -10, "Q": 10}
            if self.recall(code):
                return True
            if self.mode == "group" and code in ("H", "P"):
                # In the group box, ↑ ↓ switch among the groups that already exist.
                order, _ = self.custom()
                if order:
                    pos = order.index(self.prompt) if self.prompt in order else (-1 if code == "P" else 0)
                    self.prompt = order[(pos + (1 if code == "P" else -1)) % len(order)]
                return True
            if code in moves:
                self.cursor += moves[code]
                self.pscroll = 0
            elif code == "M":
                # Right arrow does the same as Enter (open the session or
                # collapse the group), except when you are typing.
                if not (self.mode in ("reply", "find", "group", "rename") or self.prompt):
                    return self.key("\r")
            elif code in ("U", "D"):
                if not (self.mode in ("reply", "find", "group", "rename") or self.prompt):
                    self.move_group(item, -1 if code == "U" else 1)
            elif code == "\x98":
                self.jump_group(-1)
            elif code == "\xa0":
                self.jump_group(1)
            return True

        editing = self.mode in ("reply", "find", "group", "rename") or self.prompt
        if ch == "\x1b":
            if self.mode == "find":
                self.find, self.mode = "", "task"
            elif self.mode in ("reply", "group", "rename"):
                editing_q = getattr(self, "qediting", None) if self.mode == "reply" else None
                self.prompt, self.mode, self.reply_to = "", "task", None
                if editing_q:   # editing one from the queue: esc keeps the old text and goes back to the queue
                    self.qediting = None
                    items = self.waitlist.get(editing_q[0]) or []
                    if editing_q[1] in items:
                        self.qedit = {"id": editing_q[0], "sel": items.index(editing_q[1])}
            elif self.prompt or self.compose_new:
                self.prompt, self.compose_new = "", False
            else:
                # On a working session, esc stops its work (like esc
                # inside Claude Code); without that, it asks whether to close the program.
                target = self.popup_target() if self.view == "kanban" else None
                row = target[0] if target else (item[1] if item and item[0] == "row" else None)
                if row and row.get("busy"):
                    return self.ask_stop(row)
                return self.ask_quit()
        elif ch == "\x03":
            return self.ask_quit()
        elif ch == "\r":
            if self.mode == "find":
                self.mode = "task"
            elif self.mode == "rename":
                name, what = " ".join(self.prompt.split()), self.reply_to
                self.prompt, self.mode, self.reply_to = "", "task", None
                if name and isinstance(what, tuple):
                    self.rename_group(what[1], name)
                elif name and what and name != what["name"]:
                    self.names[what["id"]] = name   # already shows with the new name in the list
                    self.save_config()
                    old, what["name"] = what["name"], name
                    self.notice, self.notice_until = f"{old} → {name}", time.time() + 6
                    self.rename_session(what, name)
            elif self.mode == "group":
                name, row = " ".join(self.prompt.split()), self.reply_to
                self.prompt, self.mode, self.reply_to = "", "task", None
                if row:
                    self.set_group([row["id"]], name)
                    self.notice = f"{row['name']} → {name}" if name else f"{row['name']} left the group"
                    self.follow(row["id"])
            elif self.mode == "reply" and getattr(self, "qediting", None) and self.prompt.strip():
                self.save_queued_edit()   # editing one from the queue: enter saves it in place
                self.prompt, self.mode, self.reply_to = "", "task", None
            elif self.mode == "reply":
                self.sync_reply_target()   # the arrow may have come along with no drawing in between
                text, row = self.prompt.strip(), self.reply_to
                self.prompt, self.mode, self.reply_to = "", "task", None
                fresh = next((r for r in self.rows if r["id"] == row["id"]), row) if row else None
                if text and fresh and fresh.get("questions") and self.dialog_of(fresh):
                    # Question open on the session screen: the text becomes its answer.
                    self.background(self.answer_question, fresh, text)
                elif text and row:
                    text = self.with_images(self.answer_by_number(row, text))
                    self.background(self.reply, row, text)
                    if self.view == "focus":
                        self.focus_step(1)
            elif self.prompt.strip() and self.view == "board":
                text, self.prompt = self.prompt.strip(), ""
                if self.bside:
                    self.new_goal(text)
                else:
                    self.new_task(text)
            elif self.prompt.strip():
                task, self.prompt, self.compose_new = self.with_images(self.prompt.strip()), "", False
                self.background(self.new_session, task)
            elif item and item[0] == "head":
                self.collapsed ^= {item[1]}
            elif item:
                self.attach(item[1])
        elif ch == "\n":
            if self.mode != "find":
                self.insert_text("\n")
        elif ch == "\x17":   # Ctrl+W (and Ctrl+Backspace in some terminals): deletes the last word
            if self.mode == "find":
                self.find = self.find.rstrip()[: len(self.find.rstrip()) - len(self.find.rstrip().split(" ")[-1])]
            else:
                p = self.caret_pos()   # deletes the word before the cursor
                kept = self.prompt[:p].rstrip()
                kept = kept[: len(kept) - len(kept.split(" ")[-1])]
                self.prompt = kept + self.prompt[p:]
                if getattr(self, "caret", None) is not None:
                    self.caret = len(kept)
        elif ch in ("\b", "\x7f"):
            if self.mode == "find":
                self.find = self.find[:-1]
            else:
                p = self.caret_pos()   # deletes the letter before the cursor
                if p:
                    self.prompt = self.prompt[:p - 1] + self.prompt[p:]
                    if getattr(self, "caret", None) is not None:
                        self.caret = p - 1
        elif ch == "\x06":
            self.mode = "find"
        elif ch in ("\x0c", "\x0b") and self.view in ("kanban", "split"):
            row = item[1] if item and item[0] == "row" else None
            target = self.popup_target() if self.view == "kanban" else None
            row = target[0] if target else row
            if row and ch == "\x0c":
                url = localhost_of(row.get("cwd") or "")
                if url:
                    open_detached(url)
                    self.notice = f"opening {url}"
                else:
                    self.notice = "this session's localhost is not running · ask it for /localhost"
            elif row:
                arts = session_artifacts(live_turns(row.get("transcript"), limit=300))
                if arts:
                    open_detached(arts[-1])
                    self.notice = f"opening the latest artifact ({len(arts)} in the session)"
                else:
                    self.notice = "this session has not published any artifact"
        elif ch == "\x05" and self.view in ("kanban", "split"):
            self.tools_open = not self.tools_open
            self.notice = "tool output shown · ctrl+e hides" if self.tools_open else "tools on one line · ctrl+e shows output"
        elif ch == "\x19" and self.view in ("kanban", "split"):
            row = item[1] if item and item[0] == "row" else None
            target = self.popup_target() if self.view == "kanban" else None
            row = target[0] if target else row
            bang = last_bang(live_turns(row.get("transcript"), limit=300)) if row else None
            if not bang:
                self.notice = "this session has not sent any ! command to copy"
            elif copy_text("! " + bang):
                self.copied = bang
                self.notice = "command copied · paste with ctrl+v"
            else:
                self.notice = "could not copy the command"
        elif ch == "\x0f" and self.view == "kanban":
            self.peek, self.hover = not self.peek, None
            self.save_config()
            self.notice = "side conversation on" if self.peek else "side conversation off · ctrl+o turns it back on"
        elif ch == "\x0e" and self.view in ("kanban", "split"):
            if self.mode == "reply":
                self.mode, self.reply_to = "task", None
            self.compose_new = not self.compose_new
        elif ch in ("\x10", "\x14") and item and item[0] == "row":   # ctrl+p or ctrl+t
            # Pinning takes the session out of its group: the selection goes along, otherwise the
            # next Ctrl+P would land on another session.
            self.toggle_pin(item[1])
            self.follow(item[1]["id"])
        elif ch == "\x07" and self.mode == "task" and not self.prompt and self.group_of(item) and (
                self.view == "kanban" or (item and item[0] == "head")):
            # Ctrl+G on the title of one of your groups (in Kanban, in its column): ↑↓ move the whole group.
            self.start_moving(self.group_of(item))
        elif ch == "\x07" and self.mode == "group" and self.reply_to and self.custom()[1].get(self.reply_to["id"]):
            # Ctrl+G again in the group box: switches to moving the session's group.
            name = self.custom()[1][self.reply_to["id"]]
            self.prompt, self.mode, self.reply_to = "", "task", None
            self.start_moving(name)
        elif ch == "\x07" and item and item[0] == "row" and self.mode == "task" and not self.prompt:
            # Ctrl+G: puts the session in a group of yours (Stand-by, for example).
            self.mode, self.reply_to = "group", item[1]
            self.prompt = self.custom()[1].get(item[1]["id"], "")
        elif ch == "\x18" and item:
            if self.confirm is not None and self.confirm[:2] == item[:2]:
                self.confirm = None
                if item[0] == "head" and item[1].startswith("custom:"):
                    # Undoing a group of yours only removes its sessions; deletes nothing.
                    self.set_group([r["id"] for r in item[2]], "")
                    self.notice = f"group {item[1][7:]} ungrouped"
                else:
                    targets = item[2] if item[0] == "head" else [item[1]]
                    self.background(self.delete, list(targets))
            else:
                self.confirm = item
        elif ch == "?" and not editing and time.time() >= getattr(self, "paste_until", 0):
            self.help = not self.help
        elif ch == " " and not editing and item and item[0] == "row":
            self.mode, self.reply_to = "reply", self.reply_target(item[1])
        elif ch >= " ":
            if (self.view in ("kanban", "split") and self.mode == "task" and not self.prompt and not self.compose_new
                    and item and item[0] == "row"):
                self.mode, self.reply_to = "reply", self.reply_target(item[1])
            if self.mode == "find":
                self.find += ch
                self.cursor = 0
            else:
                self.insert_text(ch)
        return True

    def caret_pos(self):
        """Where the cursor is in the text (None = at the end, the normal case)."""
        c = getattr(self, "caret", None)
        return len(self.prompt) if c is None else max(0, min(c, len(self.prompt)))

    def insert_text(self, s):
        p = self.caret_pos()
        self.prompt = self.prompt[:p] + s + self.prompt[p:]
        if getattr(self, "caret", None) is not None:
            self.caret = p + len(s)
            if self.caret >= len(self.prompt):
                self.caret = None   # reached the end: back to normal

    def caret_key(self, code):
        """←/→ move one letter, home/end go to start/end, del deletes the letter at the cursor."""
        p, n = self.caret_pos(), len(self.prompt)
        if code == "K":
            p -= 1
        elif code == "M":
            p += 1
        elif code == "s":   # shift+← also moves
            p -= 1
        elif code == "t":
            p += 1
        elif code == "G":
            p = 0
        elif code == "O":
            p = n
        elif code == "S":
            if p < n:
                self.prompt = self.prompt[:p] + self.prompt[p + 1:]
            n = len(self.prompt)
        p = max(0, min(p, n))
        self.caret = None if p >= n else p

    # ------------------------------------------------ input (keyboard and mouse)

    def getch(self):
        """Next key already translated (the queue holds the second code of the arrows)."""
        while not self.queue:
            self.queue += self.read_events() or []
            if not self.queue:
                time.sleep(0.01)
        return self.queue.pop(0)

    def pending(self):
        return bool(self.queue) or self.events_waiting()

    def read_events(self):
        """Keys already translated: arrows as "\\xe0"+"H"/"P", Alt+arrow as
        "\\x00"+"\\x98"/"\\xa0", click as (column, row) counting from 1.

        Depending on the terminal, the arrow arrives as a Windows key or as text that
        starts with Esc ("\\x1b[A", "\\x1bOA" or the Windows Terminal format
        "\\x1b[38;72;0;1;0;1_"). Here everything becomes the same key; a lone Esc only counts
        when nothing comes right after it."""
        raw = self.raw_events()
        self.log_keys(raw)
        # Pasted text arrives as keys, many at once, and its line break comes
        # as Enter: it used to send the message in the middle of the paste. In a burst (and right after
        # it), Enter becomes a line break, like ctrl+j.
        out = []
        while raw:
            item = raw.pop(0)
            if item != "\x1b":
                out.append("\b" if item == "\x7f" else item)
                continue
            if not raw:
                deadline = time.time() + 0.05
                while not raw and time.time() < deadline:
                    if self.events_waiting():
                        raw += self.raw_events()
                    else:
                        time.sleep(0.005)
            if raw and raw[0] in ("v", "V"):   # Alt+V arrives as Esc + v
                raw.pop(0)
                out += ["\x00", "V"]
                continue
            if not raw or raw[0] not in ("[", "O"):
                out.append("\x1b")
                continue
            raw.pop(0)
            seq = ""
            deadline = time.time() + 0.1
            while True:
                if not raw:
                    if self.events_waiting():
                        raw += self.raw_events()
                    elif time.time() > deadline:
                        break  # truncated sequence: discard
                    else:
                        time.sleep(0.005)
                    continue
                c = raw.pop(0)
                if not isinstance(c, str) or len(c) != 1:
                    continue
                seq += c
                if c.isalpha() or c in "~_":
                    break
            out += self.decode_vt(seq)
        # Emoji (and other characters outside the basic range) arrive in two halves, one per key:
        # joins the two into one character. A loose half is what broke the screen drawing.
        merged = []
        for c in out:
            if isinstance(c, str) and len(c) == 1 and "\ud800" <= c <= "\udbff":
                self._high = c
            elif isinstance(c, str) and len(c) == 1 and "\udc00" <= c <= "\udfff":
                if getattr(self, "_high", None):
                    merged.append((self._high + c).encode("utf-16", "surrogatepass").decode("utf-16"))
                self._high = None
            else:
                merged.append(c)
        # Paste: many letters at once. Counts and replaces after decoding the keys (in
        # Windows Terminal each key arrives as a code "\x1b[…_", and tab and enter only
        # show up here). In a burst, enter becomes a line break (like ctrl+j) and tab becomes
        # space: each tab of a pasted table used to become a message in the session queue.
        now = time.time()
        typed = [c for i, c in enumerate(merged) if isinstance(c, str) and len(c) == 1 and c >= " "
                 and c != "\xe0" and not (i and merged[i - 1] in ("\xe0", "\x00"))]   # held arrows do not count
        # Each read brings few keys (one key is several events): counts over time,
        # summing the reads. Nobody types 4 letters in 60 ms; it is a paste.
        recent = [t for t in getattr(self, "typed_at", []) if now - t < 0.06] + [now] * len(typed)
        self.typed_at = recent[-50:]
        waiting = bool(merged) and _safe(self.events_waiting)   # there are still keys in the console queue
        if len(recent) >= 4 or (waiting and len(typed) >= 1):
            self.paste_until = now + 0.25
        if now < getattr(self, "paste_until", 0) and self.on("paste_guard"):
            merged = [p for c in merged for p in (["\n"] if c == "\r" else [" "] * 4 if c == "\t" else [c])]
        return merged

    def log_keys(self, raw):
        """Keeps the last 300 raw keys in keys.log (only with AGENTS_KEYLOG=1)."""
        if not raw or not KEYS_LOG:
            return
        self.key_log = (getattr(self, "key_log", []) + [f"{time.strftime('%H:%M:%S')} {raw!r}"])[-300:]
        try:
            with open(KEYS_LOG, "w", encoding="utf-8") as f:
                f.write("\n".join(self.key_log) + "\n")
        except OSError:
            pass

    def decode_vt(self, seq):
        final, params = seq[-1:], seq[:-1]
        if final == "_":  # Windows Terminal: Vk;Sc;Uc;Kd;Cs;Rc
            parts = [int(p) if p.isdigit() else 0 for p in params.split(";")] + [0] * 6
            vk, _, uc, down, state, repeat = parts[:6]
            if not down:
                return []
            keys = []
            for _ in range(max(1, repeat)):
                if vk in VK_ARROWS:
                    code = VK_ARROWS[vk]
                    if (state & 0xC) and code in CTRL_ARROWS:
                        keys += ["\xe0", CTRL_ARROWS[code]]
                    elif (state & 0x3) and code in ALT_ARROWS:
                        keys += ["\x00", ALT_ARROWS[code]]
                    elif (state & 0x10) and code in SHIFT_ARROWS:
                        keys += ["\xe0", SHIFT_ARROWS[code]]
                    else:
                        keys += ["\xe0", code]
                elif vk == 0x56 and (state & 0x3) and not (state & 0xC):   # Alt+V
                    keys += ["\x00", "V"]
                elif vk == 0x0D and (state & 0x10):   # Shift+Enter: new line, like ctrl+j
                    keys.append("\n")
                elif uc:
                    keys.append("\b" if uc == 0x7F else chr(uc))
            return keys
        if params.startswith("<") and final in "Mm":
            nums = [int(n) for n in params[1:].split(";") if n.isdigit()]
            if len(nums) == 3:
                button, x, y = nums
                if final == "M" and button == 0:
                    return [("down", x, y)]
                if final == "m" and button == 0:
                    return [("up", x, y)]
                if final == "M" and button in (32, 35):
                    # Mouse moving: 32 dragging with the button pressed (selection), 35 with no button
                    # (pop-up). Without this the drag did not arrive and the selection stayed empty.
                    return [("move", x, y)]
                if button in (64, 65):
                    return ["\xe0", "W" if button == 64 else "w"]
            return []
        alt = params.endswith(";3")
        shift = params.endswith(";2")
        ctrl = params.endswith(";5")
        simple = {"A": "H", "B": "P", "C": "M", "D": "K"}
        if final in simple:
            code = simple[final]
            if ctrl and code in CTRL_ARROWS:
                return ["\xe0", CTRL_ARROWS[code]]
            if alt and code in ALT_ARROWS:
                return ["\x00", ALT_ARROWS[code]]
            if shift and code in SHIFT_ARROWS:
                return ["\xe0", SHIFT_ARROWS[code]]
            return ["\xe0", code]
        if final == "~":
            return {"5": ["\xe0", "I"], "6": ["\xe0", "Q"], "3": ["\xe0", "S"]}.get(params.split(";")[0], [])
        return []

    def events_waiting(self):
        count = ctypes.c_uint32()
        ctypes.windll.kernel32.GetNumberOfConsoleInputEvents(self.handle, ctypes.byref(count))
        return count.value > 0

    def raw_events(self):
        """Raw Windows console events (key or mouse)."""
        records = (INPUT_RECORD * 32)()
        read = ctypes.c_uint32()
        ctypes.windll.kernel32.ReadConsoleInputW(self.handle, records, 32, ctypes.byref(read))
        out = []
        for rec in records[: read.value]:
            if rec.EventType == KEY_EVENT and rec.Event.KeyEvent.bKeyDown:
                k = rec.Event.KeyEvent
                alt = k.dwControlKeyState & 0x3
                shift = k.dwControlKeyState & 0x10
                ctrl = k.dwControlKeyState & 0xC
                for _ in range(max(1, k.wRepeatCount)):
                    if k.wVirtualKeyCode in VK_ARROWS:
                        code = VK_ARROWS[k.wVirtualKeyCode]
                        if ctrl and code in CTRL_ARROWS:
                            out += ["\xe0", CTRL_ARROWS[code]]
                        elif alt and code in ALT_ARROWS:
                            out += ["\x00", ALT_ARROWS[code]]
                        elif shift and code in SHIFT_ARROWS:
                            out += ["\xe0", SHIFT_ARROWS[code]]
                        else:
                            out += ["\xe0", code]
                    elif k.wVirtualKeyCode == 0x56 and alt and not ctrl:   # Alt+V
                        out += ["\x00", "V"]
                    elif k.wVirtualKeyCode == 0x74:   # F5: updates the program
                        out.append("F5")
                    elif k.wVirtualKeyCode == 0x71:   # F2: next session waiting for you
                        out.append("F2")
                    elif k.wVirtualKeyCode == 0x09 and shift:   # shift+tab: switches the session's mode
                        out.append("S-TAB")
                    elif k.wVirtualKeyCode == 0x0D and shift:   # Shift+Enter: new line, like ctrl+j
                        out.append("\n")
                    elif k.uChar and k.uChar != "\x00":  # Shift, Ctrl alone come with 0
                        out.append(k.uChar)
            elif rec.EventType == MOUSE_EVENT:
                m = rec.Event.MouseEvent
                x, y = m.dwMousePosition.X + 1, m.dwMousePosition.Y + 1
                if m.dwEventFlags == 0 and m.dwButtonState & 1:
                    out.append(("down", x, y))
                elif m.dwEventFlags == 0 and not m.dwButtonState:
                    out.append(("up", x, y))
                elif m.dwEventFlags & 0x1:  # mouse moving (with no button or dragging)
                    out.append(("move", x, y))
                elif m.dwEventFlags & 0x2:  # double click
                    out.append(("dbl", x, y))
                elif m.dwEventFlags & 0x4:  # mouse wheel
                    up = ctypes.c_int16(m.dwButtonState >> 16).value > 0
                    out += ["\xe0", "W" if up else "w"]
        return out

    def read_input(self):
        return self.getch()

    def keep_mouse(self):
        """Re-enables the mouse on every draw. Another program in the same console (the claude that
        sends its answer, the return from ctrl+r) may turn it off; then the wheel arrives as
        an arrow and switches session instead of scrolling the conversation."""
        if not getattr(self, "mouse_on", False):   # turned off on purpose (inside a session)
            return
        mode = ctypes.c_uint32()
        ctypes.windll.kernel32.GetConsoleMode(self.handle, ctypes.byref(mode))
        want = (mode.value | 0x0080 | 0x0010 | 0x0008) & ~(0x0040 | 0x0001 | 0x0200)
        if mode.value != want:
            ctypes.windll.kernel32.SetConsoleMode(self.handle, want)
        sys.stdout.write(ESC + "?1003h" + ESC + "?1006h")

    def console(self, on):
        """Turns the console mouse on/off (off before attach, back on on return)."""
        self.handle = ctypes.windll.kernel32.GetStdHandle(-10)
        if on:
            mode = ctypes.c_uint32()
            ctypes.windll.kernel32.GetConsoleMode(self.handle, ctypes.byref(mode))
            self.input_mode = mode.value
            # Mouse and window on; quick edit, system Ctrl+C and VT input
            # off, so arrows and click arrive as Windows events.
            new = (mode.value | 0x0080 | 0x0010 | 0x0008) & ~(0x0040 | 0x0001 | 0x0200)
            ctypes.windll.kernel32.SetConsoleMode(self.handle, new)
            # Windows Terminal only passes the click if the program asks for the mouse (without it,
            # only the wheel arrives, turning into arrows): 1003 = click and motion, 1006 = long coordinates.
            sys.stdout.write(ESC + "?1003h" + ESC + "?1006h")
            sys.stdout.flush()
            self.mouse_on = True
        elif self.input_mode is not None:
            self.mouse_on = False
            sys.stdout.write(ESC + "?1003l" + ESC + "?1006l")
            sys.stdout.flush()
            ctypes.windll.kernel32.SetConsoleMode(self.handle, self.input_mode)

    def run(self):
        os.system("")  # turns on ANSI colors in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(ESC + "?1049h" + ESC + "?25l" + ESC + "2J")
        self.console(True)
        try:
            last = 0.0
            while True:
                if self.pending():
                    if not self.queue:
                        self.queue += self.read_events()
                        if not self.queue:
                            continue
                    ch = self.getch()
                    done = self.key(ch)
                    if not done:
                        break
                    # Several keys/movements together (pasted text): draws once at the end.
                    if not self.queue and done != "skip" and (not self.events_waiting() or time.time() - last > 0.1):
                        self.draw()
                        last = time.time()
                    continue
                if time.time() - last > 0.12:
                    self.tick += 1
                    self.draw()
                    last = time.time()
                time.sleep(0.02)
        except KeyboardInterrupt:
            pass
        finally:
            attach_close_all()
            self.console(False)
            sys.stdout.write(ESC + "?25h" + ESC + "?1049l")
            sys.stdout.flush()
        return RELOAD_EXIT if self.reload else 0


if __name__ == "__main__":
    # If the program crashes, the error goes to error.log and it reopens by itself
    # (except when it crashes right on opening, to avoid opening and crashing in a loop).
    started = time.time()
    try:
        sys.exit(App().run())
    except Exception:
        import traceback
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now():%d/%m %H:%M:%S}\n{traceback.format_exc()}\n")
        sys.stdout.write(ESC + "?25h" + ESC + "?1049l")
        print(f"The program crashed. The error was saved to {log}")
        sys.exit(RELOAD_EXIT if time.time() - started > 10 else 1)
