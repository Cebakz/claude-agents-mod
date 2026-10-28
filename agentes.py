"""Agentes: réplica do `claude agents` com a coluna do modelo de cada sessão.

Mesma tela, mesmos grupos, mesmas fixadas (lê e grava ~/.claude/jobs/pins.json,
o arquivo do próprio Claude) e os mesmos atalhos:

  enter     abrir a sessão (claude attach) · no título do grupo, recolher
  space     responder a sessão escolhida
  ctrl+x    apagar (aperte duas vezes) · no título, apaga o grupo inteiro
  ctrl+f    procurar          alt+↑/↓  pular de grupo
  ctrl+j    nova linha        ctrl+p   fixar / desafixar (extra desta réplica)
  ?         atalhos           esc      sair
  digitar   descreve uma tarefa; enter cria a sessão nova

Dentro da sessão aberta, Ctrl+Z volta para esta lista.
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

# Na primeira vez, instala as duas bibliotecas de fora que ele usa (o resto é da padrão).
try:
    import pyte  # noqa: F401
    import winpty  # noqa: F401
except ImportError:
    print("Instalando pywinpty e pyte (só na primeira vez)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "--disable-pip-version-check", "pywinpty", "pyte"])
    import importlib
    import site

    site.addsitedir(site.getusersitepackages())  # a pasta --user pode ter nascido agora
    importlib.invalidate_caches()

HOME = os.path.expanduser("~/.claude")
PINS = os.path.join(HOME, "jobs", "pins.json")
# Seus dados (grupos, visão, tarefas, fila) ficam fora da pasta do programa, para ele poder
# morar num repositório sem sujá-lo. Na primeira vez, traz os que estavam ao lado do script.
DATA_DIR = os.path.join(HOME, "agentes-mod")
os.makedirs(DATA_DIR, exist_ok=True)
for _name in ("grupos.json", "config.json", "tarefas.json", "fila.json"):
    _old = os.path.join(os.path.dirname(os.path.abspath(__file__)), _name)
    if os.path.exists(_old) and not os.path.exists(os.path.join(DATA_DIR, _name)):
        shutil.copy2(_old, os.path.join(DATA_DIR, _name))
GROUPS_FILE = os.path.join(DATA_DIR, "grupos.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TASKS_FILE = os.path.join(DATA_DIR, "tarefas.json")
QUEUE_FILE = os.path.join(DATA_DIR, "fila.json")   # mensagens na fila (tab)
# Registro das teclas cruas, para achar bug de teclado: só com AGENTES_KEYLOG=1 (grava tudo o
# que você digita, inclusive senha colada).
KEYS_LOG = os.path.join(DATA_DIR, "teclas.log") if os.environ.get("AGENTES_KEYLOG") == "1" else None
# Visão Projetos (estilo Paperclip): metas à esquerda, tarefas da meta à direita.
# A coluna da tarefa sai dos agentes dela; só "Feito" é você que marca.
BOARD = [("todo", "To do"), ("working", "Working"), ("review", "Review"), ("done", "Done")]
COLUMN_DOTS = ["\x1b[38;2;140;140;140m●\x1b[0m", "\x1b[38;2;90;150;235m●\x1b[0m",
               "\x1b[38;2;232;181;66m●\x1b[0m", "\x1b[38;2;177;151;252m●\x1b[0m", "\x1b[38;2;78;186;101m●\x1b[0m",
               "\x1b[38;2;215;119;87m●\x1b[0m"]
SETTINGS = os.path.join(HOME, "settings.json")
REFRESH_S = 3
MODEL = re.compile(rb'"model":"(claude-[^"]+)"')
EFFORT = re.compile(rb'"effort":"([a-z]+)"')

ESC = "\x1b["
# Modos que o Claude liga no terminal e às vezes deixa ligados ao sair da sessão:
# teclado do Windows Terminal, teclado do kitty, colar em bloco, foco e mouse.
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
RELOAD_EXIT = 3  # o agentes.cmd abre o programa de novo quando ele sai com este código

# Leitura nativa do console do Windows (ReadConsoleInputW): setas e mouse chegam
# como eventos, sem depender de como o terminal codifica a tecla.
KEY_EVENT, MOUSE_EVENT = 0x1, 0x2
VK_ARROWS = {0x26: "H", 0x28: "P", 0x21: "I", 0x22: "Q", 0x25: "K", 0x27: "M", 0x2E: "S",   # S = Delete
             0x24: "G", 0x23: "O"}   # Home, End
ALT_ARROWS = {"H": "\x98", "P": "\xa0"}
SHIFT_ARROWS = {"K": "s", "M": "t", "H": "U", "P": "D"}
CTRL_ARROWS = {"H": "\x8d", "P": "\x91"}  # Ctrl+↑ / Ctrl+↓: rolam a conversa ao lado  # Shift+← / Shift+→: mover o card de coluna no Kanban


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
# Sem o ✳: o Windows Terminal desenha ele como emoji verde e empurra o nome.
SPINNER = "·✢✶✻✽✻✶✢"
GROUPS = [("pinned", "Pinned"), ("needs", "Needs input"), ("working", "Working"), ("done", "Completed")]
QUEUE_AUTO_MAX = 10   # fila maior que isso não sai sozinha (foi colagem, não você)
CHECK_TITLE ="✻ Finished · not seen yet"   # último grupo: terminou e você ainda não olhou
MODEL_NAMES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}
# Tela de atalhos (botão "? Atalhos" ou a tecla ?): o que cada tecla faz, por visão.
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
# O painel de features (botão no topo): o que dá para ligar e desligar. Tudo ligado por padrão;
# a escolha fica no config.json ("features": {chave: false}). Regra: TODA feature nova entra
# aqui (uma linha nesta lista) e o código dela fica atrás de self.on("chave").
FEATURES = [   # (chave, nome, o que é, área do painel)
    ("summary", "SUMMARY", "summary at the top, by Haiku (costs tokens)", "Conversation"),
    ("prompt_time", "Prompt date and time", "on the right of your prompts", "Conversation"),
    ("code_copy", "Copy code", "🗐 at the end of each code block", "Conversation"),
    ("bang_buttons", "Copy ! commands", "click a ! command to copy it", "Conversation"),
    ("link_buttons", "Clickable links", "underlined links open in the browser", "Conversation"),
    ("file_buttons", "File buttons", "buttons for the files a message cites", "Conversation"),
    ("file_copy", "Copy file", "⧉ copy file: ctrl+v in Teams/WhatsApp attaches it", "Conversation"),
    ("md_browser", ".md in the browser", "formatted page; off opens the editor", "Conversation"),
    ("turn_line", "Working / Worked line", "✻ Working… / Worked for … at the end", "Conversation"),
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
    ("click_log", "Click log", "cliques.log, to debug lost clicks", "Sending"),
]
SHORTCUTS = [
    ["ctrl+f to find", "shift+enter for newline", "esc to quit"],
    ["alt+↑/↓ to jump groups", "ctrl+p to pin", "? to close"],
    ["ctrl+x to delete", "space to reply", "enter to open"],
    ["f5 to reload", "ctrl+g to group", "tab to switch views"],
]


# ---------------------------------------------------------------- dados

_json_text = {}   # caminho → (data e tamanho do arquivo, texto): só relê quando muda


def read_json(path, default):
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
        hit = _json_text.get(path)
        if hit is None or hit[0] != stamp:
            with open(path, encoding="utf-8-sig") as f:
                hit = _json_text[path] = (stamp, f.read())
        return json.loads(hit[1])   # objeto novo a cada vez: quem lê pode mexer nele
    except (OSError, ValueError):
        return default


def stamp(value, fallback):
    # updatedAt vem como texto ISO ("2026-10-15T23:10:00.000Z") ou número em ms.
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
    if found:   # só guarda o que achou: a sessão nova ainda não tem arquivo
        _transcripts[session_id] = found[0]
        return found[0]
    return continued_from(session_id, state.get("cwd") or "")


_continued = {}   # id da sessão → (hora, arquivo da conversa de onde ela continuou)


def continued_from(session_id, cwd):
    """Sessão aberta continuando outra (--resume): até gravar o arquivo dela, a conversa é a
    da antiga, que termina com {"type":"continued-in","continuedInSessionId": <esta>}."""
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
    # O modelo e o esforço da última resposta ficam perto do fim do transcript: lê de
    # trás para frente em blocos de 1 MB (até 32 MB) e guarda o achado enquanto o
    # arquivo não mudar.
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
    """Hora (ms) da última mensagem do transcript; guardada enquanto o arquivo não mudar."""
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
            # Só mensagem de verdade: linhas de sistema (retomada, resumo) não contam.
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
    """Texto da última resposta do Claude no transcript (só os blocos de texto)."""
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
    """Arquivo da conversa (.jsonl) da sessão, o mesmo que a sessão aberta vai gravando."""
    for i, sid in enumerate((s.get("sessionId", ""), state.get("resumeSessionId"), state.get("sessionId"))):
        if sid:
            path = transcript(sid, state if i == 0 else {})
            if path and os.path.exists(path):
                return path
    return ""


def background_label(f, turns):
    """Nome legível de algo que roda por trás: a descrição que o Claude deu ao comando
    (o "description" do Bash que o abriu) ou, sem ela, o comando limpo do cd, nohup,
    variáveis e redirecionamentos, com os caminhos só pelo nome do arquivo."""
    label = " ".join(str(f.get("label") or "").split())
    if f.get("kind") != "shell":
        return label
    head = label.rstrip("…").strip()
    shells = [t for t in reversed(turns) if t[0] == "tool" and t[1] in ("Bash", "PowerShell")
              and isinstance(t[4], dict) and t[4].get("description")]
    # O resultado do Bash em segundo plano cita o id ("…in background with ID: b0hgh6y18");
    # sem ele, o comando inteiro tem que bater (o começo, "cd pasta &&", é igual em vários).
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
    cmd = re.sub(r'"?[A-Za-z]:[\\/][^"\s]*[\\/]([^"\\/\s]+)"?', r"\1", cmd)   # caminho → só o nome
    return cmd.strip() or label


def tool_label(name, data):
    """Cabeçalho da ferramenta como a sessão mostra: Bash(comando), Read(arquivo)…"""
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
    """Seus textos numa linha da conversa: mensagem normal, texto colado (que vem entre
    <pasted_content>) e mensagem que entrou no meio do trabalho (queued_command)."""
    if entry.get("isMeta") or entry.get("isSidechain"):
        return []
    att = entry.get("attachment")
    if isinstance(att, dict) and att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
        texts = [att["prompt"]]
    elif entry.get("type") == "system" and entry.get("subtype") == "local_command":
        # Comando de barra numa sessão em segundo plano: gravado como linha de sistema.
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
        if bang:   # "! comando" que você mandou
            text = "! " + bang.group(1).strip()
        name = COMMAND_NAME.search(text) if text.startswith("<command-") else None
        if name:   # comando de barra: gravado como <command-name>/x</command-name><command-args>…
            args = COMMAND_ARGS.search(text)
            text = (name.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()
        if text and not text.startswith("<") and not text.startswith("This session is being continued"):
            out.append(text)
    return out


def live_turns(path, limit=60):
    """Conversa lida direto do arquivo que a sessão grava enquanto trabalha:
    ("você", texto), ("claude", texto) ou ("tool", nome, cabeçalho, resultado, entrada).
    O resultado fica None enquanto a ferramenta não terminou (uma pergunta esperando você)."""
    try:
        stat = os.stat(path)
    except (OSError, TypeError):
        return []
    # Normalmente só os últimos 8 MB; ao pular para um prompt antigo pelo índice, lê desde
    # ele (_window) e fica parado nesse trecho enquanto você lê: reler 100 MB a cada
    # escrita da sessão travaria a tela.
    start = _window.get(path)
    key = (stat.st_mtime, stat.st_size, start)
    # Um de cada vez: a tela e o pré-carregamento por trás leem o mesmo arquivo, e dois
    # lendo o mesmo pedaço novo duplicavam a conversa.
    with _live_lock:
        cached = _live.get(path)
        if cached and (cached[0] == key or (start is not None and cached[0][2] == start)):
            return cached[1][-limit:]
        inc = _inc.get(path)
        if start is None and inc and inc["offset"] <= stat.st_size:
            # Só o pedaço novo: reler 8 MB a cada linha que a sessão escreve travava a tela.
            try:
                with open(path, "rb") as f:
                    f.seek(inc["offset"])
                    chunk = f.read()
            except OSError:
                return inc["turns"][-limit:]
            cut = chunk.rfind(b"\n") + 1   # linha pela metade fica para a próxima vez
            if cut:
                # split("\n"), não splitlines(): texto colado pode ter   e outros
                # separadores dentro do json, e splitlines cortava a linha no meio.
                _parse_turns(chunk[:cut].decode("utf-8", "replace").split("\n"), inc["turns"], inc["tools"], inc["meta"])
                inc["offset"] += cut
            turns, meta = inc["turns"], inc["meta"]
        else:
            try:
                with open(path, "rb") as f:
                    pos = max(0, stat.st_size - 8_000_000) if start is None else max(0, min(start, stat.st_size - 8_000_000))
                    f.seek(pos)
                    data = f.read()   # o arquivo pode ter crescido desde o stat: vale pos + o que leu
            except OSError:
                return []
            cut = data.rfind(b"\n") + 1
            lines = data[:cut].decode("utf-8", "replace").split("\n")
            if pos > 0:
                lines = lines[1:]   # começou no meio de uma linha
            turns, tools, meta = [], {}, {"prompts": 0}
            _parse_turns(lines, turns, tools, meta)
            if start is None:
                _inc[path] = {"offset": pos + cut, "turns": turns, "tools": tools, "meta": meta}
        # Guarda tudo o que leu e devolve só o fim pedido (o índice pede mais ao pular para trás).
        _live[path] = (key, turns)
        _meta[path] = meta
        return turns[-limit:]


_live_lock = threading.Lock()


_inc = {}   # caminho → leitura em andamento: até onde já leu, conversa, ferramentas, dados


def _parse_turns(raw, turns, tools, meta):
    """Junta as linhas do arquivo da conversa em turns (continua de onde parou)."""
    for line in raw:
        if len(line) > 150_000:   # resultado de ferramenta com imagem
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
            # /compact (ou a compactação automática): uma linha na conversa, e o contexto
            # passa a ser o que sobrou, até a próxima resposta trazer o número novo.
            cm = entry.get("compactMetadata") or {}
            pre, post = cm.get("preTokens"), cm.get("postTokens")
            meta["compacted"] = entry.get("timestamp") or ""
            if post:
                meta["used"] = post
            turns.append(("claude", "*── conversa compactada" + (
                f" · {tokens_short(pre)} → {tokens_short(post)}" if pre and post else "") + " ──*"))
            continue
        if entry.get("type") == "assistant" and isinstance(message.get("usage"), dict):
            # Mesma conta do mod linha-status: contexto da última resposta, cache e modelo.
            u = message["usage"]
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
            texts = []   # depois da compactação o Claude Code regrava o "/compact": já está na conversa
        if texts and entry.get("type") == "user" and not texts[0].startswith("! "):
            # Pedido seu começa um turno novo (o "! comando" e a mensagem do meio, não).
            meta["turn_start"], meta["turn_tokens"], meta["turn_end"] = entry.get("timestamp"), 0, None
            meta["turn_done"] = None
        if entry.get("type") == "system" and entry.get("subtype") == "turn_duration":
            # A vez do Claude acabou (mesmo com comando ainda rodando por trás).
            meta["turn_done"] = entry.get("timestamp") or True
        if entry.get("type") == "user" and isinstance(entry.get("permissionMode"), str):
            meta["mode"] = entry["permissionMode"]
        if entry.get("type") == "assistant" and isinstance(message.get("usage"), dict):
            meta["turn_tokens"] = (meta.get("turn_tokens") or 0) + (message["usage"].get("output_tokens") or 0)
            meta["turn_end"] = entry.get("timestamp")
            meta["turn_done"] = None   # voltou a trabalhar (ex.: o comando de trás terminou e o acordou)
        if texts:
            for text in texts:
                turns.append(("você", text, entry.get("timestamp")))   # a hora aparece no balão
                meta["prompts"] += 1
        stdout = entry.get("content") if entry.get("subtype") == "local_command" else None
        if isinstance(stdout, str) and stdout.startswith("<local-command-stdout>"):
            # O que o comando de barra respondeu (/context, /cost, comando de mod…).
            text = ANSI.sub("", re.sub(r"</?local-command-stdout>", "", stdout)).strip("\n")
            if text.strip() and text.strip() != "(no content)":   # o /exit responde "(no content)"
                turns.append(("claude", "```\n" + text.replace("```", "'''") + "\n```"))
        if entry.get("type") == "user" and "<bash-stdout>" in str(content):
            raw = content if isinstance(content, str) else " ".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict))
            out = BASH_OUT.search(raw)
            if out:
                cmd = next((t[1][2:] for t in reversed(turns) if t[0] == "você" and t[1].startswith("! ")), "")
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
_prompts = {}   # caminho → (bytes já lidos, prompts, byte onde cada um começa)
_window = {}    # caminho → byte de onde a conversa é lida (pulo do índice para trás)


_rename_lock = threading.Lock()
ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")


class Attached:
    """Uma sessão viva aberta por um terminal escondido (claude attach). Fica
    aberta enquanto você escreve, para o Enter mandar na hora."""

    def __init__(self, row):
        from winpty import PtyProcess
        import pyte
        self.id, self.row, self.out, self.cut, self.used = row["id"], row, [], 0, time.time()
        self.lock, self.ready, self.dead = threading.Lock(), threading.Event(), threading.Event()
        # Cópia da tela da sessão: é dela que sai a pergunta/permissão aberta.
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
        """A tela da sessão, só a parte da conversa (a coluna da direita é outro painel)."""
        with self.slock:
            if self.cached[0] != self.version:
                self.cached = (self.version, [l[:100].rstrip() for l in self.screen.display])
            return list(self.cached[1])

    def dialog(self):
        """Linhas da pergunta ou do pedido de permissão aberto na sessão; [] se não tem."""
        lines = self.lines()
        marks = ("Esc to cancel", "Enter to select", "Submit answers", "Do you want to proceed")
        end = next((i for i in range(len(lines) - 1, -1, -1) if any(m in lines[i] for m in marks)), None)
        if end is not None:   # as opções que vêm logo abaixo da marca (ex.: "2. Cancel")
            while end + 1 < len(lines) and re.match(r"\s*(❯\s*)?\d+\. ", lines[end + 1]):
                end += 1
        if end is None:
            return []
        # A caixa de digitar logo abaixo (❯ sem número de opção): a marca era texto da conversa
        # (ex.: a saída de um comando que cita "Esc to cancel"), não uma janela aberta.
        if any(re.match(r"\s*❯(?!\s*\d+\.)", l) for l in lines[end + 1:]):
            return []
        # O começo é a linha ─── de cima da janela. A pergunta (AskUserQuestion) tem outra ───
        # no meio, antes de "5. Chat about this" (ou "Chat about this", sem número, quando as
        # opções vêm ao lado de uma prévia): essa não conta.
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
        """O modo que a sessão mostra embaixo: "auto mode", "plan mode", "accept edits"…"""
        for line in reversed(self.lines()[-8:]):
            m = MODE_RE.search(line)
            if m:
                return m.group(2).strip()
            if "? for shortcuts" in line:
                return "manual mode"
        return ""

    def waiting_you(self):
        """A sessão diz que tem pergunta ou permissão aberta (mesmo que a tela não mostre)."""
        return bool(self.row.get("questions") or self.row.get("permission"))

    def _box(self):
        """O texto da caixa de digitar (entre as duas últimas linhas ───); None sem caixa."""
        lines = self.lines()
        rules = [i for i, l in enumerate(lines) if l.startswith("─" * 30)]
        if len(rules) < 2:
            return None
        return " ".join(l.strip() for l in lines[rules[-2] + 1:rules[-1]])

    def _refocus(self):
        """Esc devolve o teclado à caixa (painel de mod focado) e ctrl+u limpa a linha. Só com
        a sessão parada: trabalhando, o esc interromperia o trabalho dela."""
        if self.row.get("busy"):
            return False
        self.p.write("\x1b")
        time.sleep(0.4)
        self.p.write("\x15")
        time.sleep(0.4)
        return True

    def press(self, key):
        """Uma tecla na pergunta/permissão (o número da opção)."""
        with self.lock:
            if self.ready.wait(5) and not self.dead.is_set():
                try:
                    self.p.write(key)
                except Exception:   # "Pty is closed": a conexão morreu sem avisar
                    self.dead.set()
                    return False
                self.used = time.time()
                return True
            return False

    def _starting(self):
        """O attach ainda está acordando a sessão (mostra só a conversa, sem a caixa de verdade)."""
        return any("Session is starting" in l for l in self.lines()[-4:])

    def _warm(self):
        # A caixa (❯) aparece logo, mas as teclas só valem uns 0,7 s depois, e
        # isso varia. Cola uma marca única até ela aparecer na tela; aí apaga.
        # Com pergunta/permissão aberta não digita nada: número ali escolhe opção.
        limit = time.time() + (12 if self.row.get("alive") else 90)   # fechada: o attach a reabre antes
        while time.time() < limit and not self.dead.is_set():
            if self.dialog() or (self.waiting_you() and "❯" in ANSI.sub("", "".join(self.out))):
                time.sleep(0.8)
                self.ready.set()
                return
            # Sessão fechada acordando: o attach mostra a conversa com uma caixa ❯ de enfeite
            # ("Session is starting — showing its transcript") até ela subir, o que pode passar
            # de 15 s. Essa caixa não recebe teclas: só conta a ❯ depois que o aviso sai.
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
            # Marca só de letras (sem número, sem s/n): nunca escolhe opção nenhuma.
            probe = "qz" + "".join(letters[int(c)] for c in f"{os.getpid() % 1000}{tries}") + "q"
            n = self._mark()
            self.p.write("\x1b[200~" + probe + "\x1b[201~")
            end = time.time() + 0.25
            while time.time() < end and not self._seen(probe, n):
                time.sleep(0.02)
            if self._seen(probe, n):
                self.p.write("\x15")   # ctrl+u: limpa a linha (as marcas de teste)
                # Logo depois da primeira tecla a sessão ainda perde o que chega: folga curta.
                time.sleep(0.4)
                if probe in (self._box() or "").replace(" ", ""):
                    self._refocus()   # o ctrl+u não chegou: o teclado está num painel do lado
                self.ready.set()
                return
        self.close()

    def send(self, text, wait=15):
        """Cola e dá Enter. Devolve (True, "") se a mensagem entrou na conversa,
        (False, motivo) se nem digitou, (None, motivo) se digitou sem confirmar."""
        with self.lock:
            if not self.ready.wait(wait) or self.dead.is_set():
                return False, "the session did not open in time"
            # As duas marcas juntas, na caixa e no rodapé: o mesmo texto citado numa conversa não conta.
            tail = [l.strip() for l in self.lines()[-6:]]
            if "❯ describe a task for a new session" in tail and any(l.startswith("enter to return · space to reply") for l in tail):
                # A conexão saiu da sessão e está na lista do claude agents: o texto viraria
                # uma sessão nova. Fecha; quem chamou abre outra.
                self.close()
                return False, "the hidden connection had left the session"
            if self.dialog() or self.waiting_you():
                # Enter ali aprovaria a opção marcada: não digita nada.
                return False, "it is waiting on a question or permission: press the option number"
            path = self.row.get("transcript") or ""
            start = os.path.getsize(path) if path and os.path.exists(path) else 0
            first = text.strip().splitlines()[0]
            if first.startswith("!"):   # "! comando" é gravado como <bash-input> comando
                first = first[1:].strip()
            needle = json.dumps(first[:40], ensure_ascii=False)[1:-1].encode("utf-8")
            # O que aparece na caixa: o começo do texto (sem espaços, que a tela
            # pode quebrar) ou "Pasted text", quando o texto é grande.
            head = "".join(text.split())[:12]

            def echoed(since):
                # O texto tem que estar DENTRO da caixa (entre as duas últimas ───). Em outro
                # lugar da tela não vale: com o painel de arquivos focado, o "/" abre a busca dele
                # e o texto ia para lá; o Enter não mandava nada e aparecia "✓ sent".
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
                # A caixa de digitar (entre as duas últimas linhas ───) ficou sem o texto: a
                # sessão aceitou. Vale para o "! comando", que só é gravado quando termina.
                box = self._box()
                if box is None:
                    return False
                box = "".join(box.split())
                return head not in box and "Pastedtext" not in box

            for round_ in range(2):
                # 1) Cola até o texto aparecer na caixa (a sessão às vezes perde a colagem).
                for paste_try in range(6):
                    if paste_try in (2, 4):
                        # Duas colagens e nada na caixa: o teclado está num painel do lado (ex.:
                        # busca da árvore de arquivos). Esc fecha a busca / devolve o foco à caixa.
                        self._refocus()
                    n = self._mark()
                    self.p.write("\x15\x1b[200~" + text + "\x1b[201~")
                    end = time.time() + 0.6
                    while time.time() < end and not echoed(n):
                        time.sleep(0.02)
                    if echoed(n):
                        break
                else:
                    self.p.write("\x15")
                    return False, "the session did not accept the text"
                time.sleep(0.05)
                # 2) Enter até a mensagem entrar na conversa. Enter com a caixa vazia não faz nada.
                self.used = time.time()
                for attempt in range(5):
                    if attempt and self.dialog():   # já abriu uma permissão: Enter aprovaria
                        return True, ""
                    self.p.write("\r")
                    end = time.time() + (0.8 if attempt < 4 else 4)
                    while time.time() < end:
                        if arrived():
                            return True, ""
                        time.sleep(0.05)
                    if box_empty():
                        return True, ""
                # O texto entrou e o Enter não: o teclado da sessão está num painel do lado
                # (ex.: a árvore de arquivos, depois de um clique). Esc devolve o foco à caixa;
                # parada, o esc não interrompe nada. Limpa o que ficou e tenta mais uma vez.
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
    """Uma linha da pergunta/permissão copiada da sessão, com cor: a marcada em laranja,
    a pergunta em negrito, a explicação de cada opção apagada."""
    if any(m in l for m in ("Enter to select", "Esc to cancel")):
        return ""   # a dica de teclas da sessão: a do mod fica no título
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
    """Abre (ou reaproveita) o terminal escondido da sessão. Fecha os parados há
    mais de 2 min, e no máximo 3 ficam abertos."""
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
    # Sessão fechada: o attach a reabre (mesmo id, mesma conversa), o que leva uns segundos.
    try:
        return conn.send(text, wait=15 if row.get("alive") else 60)
    except Exception:
        # O terminal escondido morreu sem avisar (ex.: depois de cancelar a mensagem, o
        # "Pty is closed"): joga fora, abre outro e manda de novo, uma vez.
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
PATH_QUOTED = re.compile(r"`([A-Za-z]:[\\/][^`\n]+?)`")          # entre crases pode ter espaço
PATH_BARE = re.compile(r"(?<![\w`])([A-Za-z]:[\\/][^\s`'\"<>|*?]+)")
_exists = {}


# O que vale um botão no fim da mensagem: o que se abre para ver (imagem, página, texto
# formatado, vídeo). Código e pasta não: esses você lê no editor, não aqui.
VIEW_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".html", ".htm", ".md",
            ".markdown", ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v",
            # Documentos que se mandam para alguém (Excel, Word, PowerPoint, PDF). Sem .xlsm, .xls,
            # .docm e .doc: esses podem trazer macro.
            ".xlsx", ".csv", ".docx", ".pptx", ".pdf", ".txt")   # pasta também (abre no Explorer)


def message_files(text, exts=None):
    """Arquivos e pastas que a mensagem cita e que existem nesta máquina, na ordem
    em que aparecem (sem repetir). "caminho:linha" vira só o caminho. Com exts, só
    as pastas e os arquivos com essas extensões."""
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
    """Prompt como aparece na tela: a imagem colada vira só [Imagem #n], sem o caminho."""
    return IMAGE_TAG.sub(r"[Image #\1]", text)


_conv_texts = {}   # caminho → (bytes já lidos, prompts até ali, [(número do prompt, quem, texto)])


def conversation_texts(path):
    """Seus textos e os do Claude, com o número do seu prompt em que cada um está
    (mesma conta do índice). Lê o arquivo uma vez e depois só o que a sessão escreveu."""
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
            found.append((count, "você", text))
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            for c in (entry.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                    found.append((count, "claude", c["text"]))
    if len(_conv_texts) > 200:   # não guarda para sempre conversa de sessão apagada
        _conv_texts.pop(next(iter(_conv_texts)))
    _conv_texts[path] = (done + cut, count, found)
    return found


def search_conversations(rows, needle, per_session=60):
    """Onde a palavra aparece em cada conversa: {id: [(nº do prompt, quem, trecho)]}."""
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
    """Todos os seus prompts da sessão, do arquivo inteiro (a conversa ao vivo só lê os
    últimos 8 MB). Lê uma vez e depois só o que a sessão escreveu; mesmo filtro do live_turns."""
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
    cut = chunk.rfind(b"\n") + 1   # linha pela metade fica para a próxima leitura
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


_prompt_times = {}   # caminho → hora de cada prompt (mesma ordem do all_prompts)


def prompt_when(path, n):
    """"21/10 17:40" de quando você mandou o prompt n (1 = o primeiro); "" sem hora."""
    times = _prompt_times.get(path) or []
    ms = stamp(times[n - 1], 0) if 0 < n <= len(times) and times[n - 1] else 0
    return datetime.fromtimestamp(ms / 1000).strftime("%d/%m %H:%M") if ms else ""


def prompt_offset(path, n):
    """Byte onde começa o prompt n (1 = o primeiro) no arquivo da sessão."""
    all_prompts(path)
    spots = _prompts.get(path, (0, [], []))[2]
    return spots[n - 1] if 0 < n <= len(spots) else None


_meta = {}
_slow = {}   # resultados de git / netstat guardados por alguns segundos


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
    """Contexto, cache e modelo da última resposta, lidos só do fim do transcript
    (rápido o bastante para todas as sessões a cada atualização)."""
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


USAGE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "linha-uso")


_usage = [0.0, ""]


def plan_usage():
    if time.time() - _usage[0] < 5:
        return _usage[1]
    _usage[:] = [time.time(), _plan_usage()]
    return _usage[1]


def _plan_usage():
    """Consumo do plano (5h, 7d), gravado pelo mod linha-status de cada sessão aberta
    (um arquivo por sessão). Sessão parada guarda o número da última resposta dela, que
    fica velho; como o consumo só sobe até a janela zerar, vale a janela mais nova e,
    dentro dela, o maior número."""
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
        # 5h volta hoje (só a hora); 7d volta noutro dia (dia da semana e hora).
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
    """Janela de contexto do modelo: 1M nos 5.x e em quem roda com [1m]; 200k nos outros."""
    m = re.search(r"(opus|sonnet|haiku|fable)-(\d+)", model or "")
    if "[1m]" in (model or "") or (m and m.group(1) != "haiku" and int(m.group(2)) >= 5):
        return 1_000_000
    return 200_000


RESUMOS_FILE = os.path.join(HOME, "jobs", "agentes-resumos.json")
_resumo_lock = threading.Lock()
_resumo_rodando = set()


def session_brief(row, turns):
    """Resumo da sessão inteira (do que ela trata e onde está), feito pelo Haiku a partir
    dos seus pedidos e da última resposta. Fica guardado e só é refeito quando chega um
    pedido novo ou a sessão muda de estado; enquanto isso, vale o último."""
    asks = [t[1] for t in turns if t[0] == "você"]
    answer = next((t[1] for t in reversed(turns) if t[0] == "claude"), "")
    key = f"lang:{len(all_prompts(row.get('transcript'))) or len(asks)}:{row.get('group')}"   # lang: refaz os antigos em inglês
    saved = read_json(RESUMOS_FILE, {})
    hit = saved.get(row["id"]) if isinstance(saved, dict) else None
    if (not hit or hit.get("key") != key) and asks and row["id"] not in _resumo_rodando:
        _resumo_rodando.add(row["id"])
        threading.Thread(target=_make_brief, args=(row["id"], key, asks, answer, row.get("intent") or ""),
                         daemon=True).start()
    return (hit or {}).get("text", "")


def _make_brief(row_id, key, asks, answer, intent):
    try:
        pedidos = "\n".join("- " + " ".join(a.split())[:240] for a in ([intent] if intent else []) + asks[-14:])
        prompt = (f"User requests, first to last:\n{pedidos}\n\n"
                  f"Assistant's last reply:\n{' '.join(answer.split())[:900]}")
        # O resumo sai na língua em que a sessão é conversada: sem as configurações do
        # usuário (os hooks dele mudavam o formato) e com um prompt de sistema só disso.
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
            with _resumo_lock:
                saved = read_json(RESUMOS_FILE, {})
                saved = saved if isinstance(saved, dict) else {}
                saved[row_id] = {"key": key, "text": text}
                with open(RESUMOS_FILE, "w", encoding="utf-8") as f:
                    json.dump(saved, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    finally:
        _resumo_rodando.discard(row_id)


ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[\w-]+")


def session_artifacts(turns):
    """Links dos artifacts que a sessão publicou (na ordem em que saíram)."""
    found = []
    for t in turns:
        if t[0] == "tool" and t[1] == "Artifact" and t[3]:
            for url in ARTIFACT_URL.findall(t[3]):
                if url not in found:
                    found.append(url)
    return found


def artifact_titles(turns):
    """[(nome, link)] dos artifacts da sessão, o mais recente primeiro. O nome é o título
    publicado ou o nome do arquivo; republicar o mesmo link só atualiza o nome."""
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
    """Endereço do localhost da pasta, se ele está de pé (o marker do /localhost);
    "" se não está; None se a pasta não tem o marker .claude/localhost.json."""
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
    """A linha de status em versão curta, para caber na lista ao lado da conversa:
    contexto, cache, effort e branch."""
    now = now or time.time()
    meta = row.get("meta") or {}
    # Cada parte tem largura fixa: sem o dado, fica o espaço, e as colunas não entortam.
    ctx = cache = " " * 6
    if meta.get("used"):
        pct = meta["used"] * 100 / context_window(meta.get("model") or row.get("model"))
        dot = "🟢" if pct < 50 else "🟡" if pct < 80 else "🔴"
        ctx = f"{dot}{round(pct):>3}%"
    if meta.get("ts"):
        left = stamp(meta["ts"], 0) / 1000 + meta.get("ttl", 3600) - now
        cache = "🧊" + DIM + "  --" + RESET if left <= 0 else f"🧊{'<1' if left < 60 else int(left // 60):>3}m"
    # Emoji, como o 🧊 do cache: o Windows desenha colorido com a fonte de emoji dele. Chip
    # não existe em emoji; 🧠 (modelo) e ⚡ (effort) são os mesmos da linha de status.
    # "medium" vira "med": com 6 letras ele não cabia na coluna e saía "medi…".
    effort = f"⚡ {'med' if row['effort'] == 'medium' else row['effort']}" if row.get("effort") else ""
    # Modelo na largura do maior da lista, para o espaço entre as colunas ser sempre um só.
    model = fit(model_label(row), model_w or text_width(model_label(row)))
    return f"{ctx} {cache} 🧠 {model} {effort}".rstrip()


CTX_DIR = os.path.join(HOME, "linha-contexto")   # o mod linha-status grava aqui o /context de cada sessão
# Cor de cada categoria do /context (pelo começo do nome, em minúsculas).
CTX_COLORS = [("system prompt", (110, 150, 220)), ("system tools", (100, 200, 195)), ("mcp", (165, 135, 250)),
              ("custom agents", (140, 205, 120)), ("agents", (140, 205, 120)), ("memory", (225, 200, 95)),
              ("skills", (235, 150, 200)), ("messages", (215, 110, 90))]
CTX_FREE = (52, 56, 66)


def tokens_short(n):
    return f"{n / 1_000_000:g}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n < 10_000 else f"{round(n / 1000)}k"


def compact_pct():
    """O % da janela em que o Claude compacta, do seu settings.json (env) ou do ambiente; 0 sem ajuste."""
    raw = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") or \
        str(((cached("settings-env", 60, lambda: read_json(SETTINGS, {})) or {}).get("env") or {}).get(
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") or "")
    try:
        pct = float(raw)
    except ValueError:
        return 0
    return pct if 0 < pct < 100 else 0


def context_lines(row, width):
    """A barra do contexto, como no /context: total, onde compacta, uma barra colorida por
    categoria e a legenda. Vem do arquivo que o mod linha-status grava ao fim de cada vez do
    Claude; sem ele (sessão que ainda não terminou uma vez com o mod), não aparece."""
    convo = os.path.basename(row.get("transcript") or "")[:-6]
    data = read_json(os.path.join(CTX_DIR, convo + ".json"), {}) if convo else {}
    if not isinstance(data, dict) or not data.get("max") or not data.get("rows"):
        return []
    total, top = data.get("total") or 0, data["max"]
    meta = row.get("meta") or _meta.get(row.get("transcript") or "") or {}
    if meta.get("compacted") and stamp(meta["compacted"], 0) > (data.get("ts") or 0):
        # Compactou depois do último /context gravado: as categorias antigas não valem mais.
        # Mostra só o que sobrou; o mod grava o número novo ao fim da próxima resposta.
        left = meta.get("used") or 0
        pct = round(left * 100 / top)
        head = (ORANGE + "◆ " + RESET + BOLD + tokens_short(left) + RESET + DIM + f"/{tokens_short(top)} " + RESET
                + GREEN + BOLD + f"{pct}%" + RESET + DIM + "   compactada · as categorias voltam na próxima resposta" + RESET)
        n = max(1, round(left * width / top)) if left else 0
        return [fit_ansi(head, width), ESC + "38;2;150;150;150m" + "━" * n + ESC + f"38;2;{CTX_FREE[0]};{CTX_FREE[1]};{CTX_FREE[2]}m"
                + "━" * (width - n) + RESET]
    used = [r for r in data["rows"] if r.get("kind") == "used" and (r.get("tokens") or 0) > 0]
    free = sum(r.get("tokens") or 0 for r in data["rows"] if r.get("kind") == "free")
    color = lambda name: next((c for k, c in CTX_COLORS if name.lower().startswith(k)), (150, 150, 150))
    fg = lambda c: ESC + f"38;2;{c[0]};{c[1]};{c[2]}m"
    pct = round(total * 100 / top)
    # Onde compacta: o seu ajuste (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, ex.: 60% da janela) vale mais
    # que o número do Claude Code, que ignora o ajuste e dá a janela menos a reserva.
    pct_cfg = compact_pct()
    if pct_cfg:
        data["compactAt"] = round(top * pct_cfg / 100)
    # Duas linhas só: o total e a legenda inteira numa linha (nomes curtos), e a barra embaixo.
    short = lambda name: next((s for k, s in (("system prompt", "system"), ("system tools", "tools"),
                                               ("mcp", "mcp"), ("custom agents", "agents"), ("memory", "memory"),
                                               ("skills", "skills"), ("messages", "msgs"))
                               if name.lower().startswith(k)), name.lower())
    head = (ORANGE + "◆ " + RESET + BOLD + tokens_short(total) + RESET + DIM + f"/{tokens_short(top)} " + RESET
            + (GREEN if pct < 50 else YELLOW if pct < 80 else ESC + "38;2;232;98;98m") + BOLD + f"{pct}%" + RESET
            # "┃600k": o mesmo traço amarelo da barra, com onde ele está.
            + ("  " + YELLOW + "┃" + RESET + DIM + tokens_short(data["compactAt"]) + RESET if data.get("compactAt") else ""))
    merged = {}   # categorias com o mesmo nome curto viram uma (as duas linhas de "mcp")
    for r in used:
        c, n = merged.get(short(r["name"]), (color(r["name"]), 0))
        merged[short(r["name"])] = (c, n + r["tokens"])
    items = [(c, name, n) for name, (c, n) in merged.items()] + [(CTX_FREE, "free", free)]
    # "■ system 4.9k": espaço entre o quadradinho e o nome; entre os itens, dois espaços
    # (um só quando a linha não cabe).
    cells = [fg(c) + "■" + RESET + f" {name} " + DIM + tokens_short(n) + RESET for c, name, n in items]
    for gap in ("   ", "  ", " "):   # o maior espaço entre os itens que ainda cabe na linha
        legend = gap.join(cells)
        if visible_width(head) + 3 + visible_width(legend) <= width:
            break
    lines = [fit_ansi(head + "   " + legend, width)]
    # A barra: cada categoria ocupa a parte dela da janela; o resto é o espaço livre.
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
    """Modelo curto: "claude-opus-5-5" vira "opus 5.5"."""
    meta = row.get("meta") or {}
    parts = re.findall(r"[a-z]+|\d+", (meta.get("model") or row.get("model") or "").replace("claude-", ""))
    words = [p for p in parts if not p.isdigit()][:1]
    nums = [p for p in parts if p.isdigit() and len(p) < 4][:2]
    return " ".join(words + [".".join(nums)] if nums else words) or "—"


# Quanto a sessão custaria pela API, em US$ por milhão de tokens: entrada, saída e leitura de
# cache (tabela da skill claude-api, 06/out/2026). Escrever no cache custa 1,25x a entrada
# (5 min) ou 2x (1 h). O primeiro prefixo que bate vale, por isso o mais longo vem antes.
PRICES = [("fable-5-1", 10, 50, 0.25), ("mythos-5-1", 10, 50, 0.25), ("fable", 10, 50, 1),
          ("mythos", 10, 50, 1), ("opus-5-5", 4, 20, 0.2), ("opus", 5, 25, 0.5),
          ("sonnet-5", 2, 10, 0.2), ("sonnet", 3, 15, 0.3), ("haiku-5-5", 0.1, 0.5, 0.01),
          ("haiku", 1, 5, 0.1)]
_cost = {}   # arquivo → [até onde leu, ids de resposta já contados, total em US$]


def usage_cost(model, u):
    """Preço de uma resposta, pelos tokens do usage dela; None se o modelo é desconhecido."""
    price = next((p for p in PRICES if p[0] in (model or "")), None)
    if not price:
        return None
    _, inp, out, read = price
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens") or 0
    w5m = (u.get("cache_creation_input_tokens") or 0) - w1h
    prompt = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + w5m + w1h
    scale = 5 if price[0] == "haiku-5-5" and prompt > 100_000 else 1   # Haiku 5.5 acima de 100K
    scale *= 2 if u.get("speed") == "fast" else 1                      # fast mode: preço em dobro
    cost = ((u.get("input_tokens") or 0) * inp + (u.get("output_tokens") or 0) * out
            + (u.get("cache_read_input_tokens") or 0) * read + w5m * inp * 1.25 + w1h * inp * 2)
    searches = (u.get("server_tool_use") or {}).get("web_search_requests") or 0
    return cost * scale / 1e6 + searches * 0.01                       # busca: US$ 10 por mil


def session_cost(path):
    """Custo da conversa inteira e dos subagentes dela; lê só o que o arquivo cresceu."""
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
            # Cada bloco da resposta vira uma linha com o mesmo id e o mesmo usage: conta uma vez.
            if not isinstance(message.get("usage"), dict) or message.get("id") in state[1]:
                continue
            state[1].add(message.get("id"))
            state[2] += usage_cost(message.get("model"), message["usage"]) or 0
        total += state[2]
    return total


def headless_agents(row):
    """Quantos "claude -p" (testadores, lotes) estão trabalhando na pasta da sessão ou
    embaixo dela. Um script da sessão os abre sozinho; o Claude conta só "1 shell". Cada
    um grava a conversa (entrypoint sdk-cli) na pasta de projetos da pasta dele: conta
    as escritas no último minuto. Devolve [{"label", "startedAt"}], um por "claude -p"."""
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
            label = ""   # o começo do pedido que o script deu a ele
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
    """Abre arquivo ou link no app padrão sem dar a ele esta tela: o os.startfile deixa o
    app herdar o console, e o VS Code escreve o log dele ("[main …] update#setState") por
    cima do desenho. Pelo Explorer, quem abre é ele, e o app nasce longe deste console."""
    if not os.path.exists(target) and not re.match(r"^[a-z][a-z0-9+.-]*:", str(target), re.I):
        raise OSError(f"not found: {target}")
    subprocess.Popen(["explorer", os.path.normpath(target) if os.path.exists(target) else target],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=0x08000000)


MD_PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
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
<!-- integrity: o navegador recusa o arquivo do CDN se ele não for exatamente este. -->
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
    """Uma página do arquivo .md, formatada (títulos, tabelas, código), para abrir no
    navegador. Imagem e link relativos funcionam: a página aponta para a pasta do .md."""
    import html
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    folder = os.path.join(DATA_DIR, "leitura")
    os.makedirs(folder, exist_ok=True)
    name = re.sub(r"[^\w.-]", "_", os.path.basename(path))
    out = os.path.join(folder, name + ".html")
    import pathlib
    base = pathlib.Path(os.path.dirname(os.path.abspath(path))).as_uri().rstrip("/") + "/"
    page = (MD_PAGE.replace("__TITLE__", html.escape(os.path.basename(path)))
            .replace("__BASE__", html.escape(base))
            .replace("__PATH__", html.escape(path))
            # "</" vira "<\/": o texto do .md não fecha o <script> no meio.
            .replace("__SOURCE__", json.dumps(text, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")))
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    return out


def cost_label(row):
    """"$1.23" do que a sessão custaria pela API, ou "" sem a conversa."""
    path = row.get("transcript") or ""
    if not path.endswith(".jsonl"):
        return ""
    cost = cached(("cost", path), 10, lambda: session_cost(path))
    return f"${cost:,.2f}" if cost < 1000 else f"${cost / 1000:,.1f}k"


def status_items(row, now=None):
    """A linha de status do mod linha-status, para a sessão escolhida:
    contexto, cache, branch, effort, localhost, modelo e quantos prompts."""
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
    # Mesma lógica do modelo: transcript atual, ou o anterior se o atual estiver vazio.
    for i, sid in enumerate((s.get("sessionId", ""), state.get("resumeSessionId"), state.get("sessionId"))):
        if sid:
            found = last_activity(transcript(sid, state if i == 0 else {}))
            if found:
                return found
    return None


def session_hit(s, state, pattern):
    # Uma sessão retomada começa um transcript novo, ainda sem resposta: nesse caso
    # vale o que estiver no transcript anterior da mesma sessão.
    found = last_hit(transcript(s.get("sessionId", ""), state), pattern)
    for older in (state.get("resumeSessionId"), state.get("sessionId")):
        if found or not older:
            break
        found = last_hit(transcript(older, {}), pattern)
    return found


def elapsed(state, group, fallback_ms):
    """Segundos da coluna da direita, na regra do `claude agents` (conferida em
    16/10/2026 contra as 15 sessões da tela original):
    concluída = quanto tempo levou até terminar a primeira vez (firstTerminalAt -
    createdAt); trabalhando ou esperando você = há quanto tempo foi criada."""
    created = stamp(state.get("createdAt"), fallback_ms) / 1000
    first_done = stamp(state.get("firstTerminalAt"), 0) / 1000
    if group == "done" and first_done:
        return max(0, first_done - created)
    return max(0, time.time() - created)


def duration(seconds):
    # Unidade escrita por extenso: "2m" deixava dúvida entre minuto e mês.
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    days = s // 86400
    return f"{days} dia" if days == 1 else f"{days} dias"


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
    """{pid: nome do exe} de todos os processos abaixo de root (foto rápida do Windows)."""
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
    """Histórico da sessão (~/.claude/jobs/<id>/timeline.jsonl): cada linha é um
    pedido seu (text vazio, fora de working), um passo do Claude (working) ou uma
    resposta (text com o que ele escreveu)."""
    path = os.path.join(HOME, "jobs", job_id, "timeline.jsonl")
    try:
        mtime = os.path.getmtime(path)
        cached = _timelines.get(path)
        if cached and cached[0] == mtime:   # o pop-up redesenha várias vezes por segundo
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
        if not s.get("id"):   # sessão nascendo ainda sem id: entra na próxima volta
            continue
        state = read_json(os.path.join(HOME, "jobs", s["id"], "state.json"), {})
        output = state.get("output") if isinstance(state.get("output"), dict) else {}
        # O claude agents vale mais que o state.json: depois de você aprovar, o state.json
        # pode ficar em "blocked" com a sessão já trabalhando (e ela sumia como "needs you").
        flow = s.get("state") or state.get("state") or ""
        busy = s.get("status") == "busy"
        block = state.get("block") if isinstance(state.get("block"), dict) else {}
        questions = [q for q in block.get("questions") or [] if isinstance(q, dict) and q.get("question")]
        if questions:   # está parada numa pergunta sua (AskUserQuestion)
            flow = "blocked"
        # Pergunta ou pedido de permissão aberto: o state.json segue "working", mas o
        # claude agents diz "waiting"/"blocked".
        if s.get("status") == "waiting" or s.get("state") == "blocked":
            flow = "blocked"
        # Processo parado ("idle") com o state.json ainda em "working": o turno acabou e o
        # estado ficou velho (ex.: a sessão abriu outra sessão e não atualizou o próprio).
        if s.get("status") == "idle" and flow == "working":
            flow = "done"
        needs = state.get("needs") if isinstance(state.get("needs"), str) else ""
        permission = needs[len("approve "):] if flow == "blocked" and needs.startswith("approve ") else ""
        if flow in ("blocked", "done"):
            # Esperando você ou terminou: o processo pode seguir "busy" por causa de tarefas
            # em segundo plano (comando rodando, vigia de artifact), mas o turno acabou.
            busy = False
        group = "needs" if flow == "blocked" else "working" if (flow == "working" or busy) else "done"
        if group == "working" and (_meta.get(session_path(s, state)) or {}).get("turn_done"):
            # O claude agents segue dizendo "working" enquanto um comando roda por trás, mas a
            # vez do Claude já acabou (ele volta sozinho quando o comando terminar).
            group, busy = "done", False
        inflight = state.get("inFlight") if isinstance(state.get("inFlight"), dict) else {}
        background = max(0, (inflight.get("tasks") or 0) - (inflight.get("drainableMonitors") or 0))
        if group == "done" and background and s.get("pid"):
            # Subagente ou comando ainda rodando por trás: o Claude volta quando ele acabar,
            # então não está concluída (ela aparecia pronta e o trabalho seguia).
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
            "permission": permission,   # "PowerShell: comando" quando pede sua permissão
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
            # Comandos ainda rodando por trás (o vigia de artifact não conta): "2 shells still running".
            "background": background,
            # O que roda por trás, com tipo (agent = subagente, shell = comando), nome e início.
            "fan": [f for f in state.get("fan") or [] if isinstance(f, dict) and f.get("kind")],
            # Token colado no pedido (Authorization: Bearer …) não aparece na tela.
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


# ---------------------------------------------------------------- texto

def cell_width(c):
    if unicodedata.combining(c):
        return 0
    # Símbolos que o terminal desenha como emoji (2 colunas) mesmo sendo "N" no Unicode.
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
    """Corta com … ou completa com espaços até a largura exata."""
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
    """Pedaço da linha entre as colunas a e b (contando de 0), mantendo as cores:
    os códigos de cor passam todos, as letras só dentro do pedaço."""
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
    """Como o Claude conta o tempo do turno: 16s, 3m 55s, 1h 2m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def visible_width(text):
    return text_width(ANSI.sub("", text))


# O Windows Terminal desenha Sixel numa célula "virtual" de 10x20 pixels.
CELL_W, CELL_H = 10, 20
MODE_RE = re.compile(r"(⏵⏵|⏸)\s*([a-z][a-z ]*?)\s+on\b")   # "⏵⏵ auto mode on", "⏸ plan mode on"
MODE_NAMES = {"auto": "auto mode", "default": "manual mode", "plan": "plan mode", "acceptEdits": "accept edits on",
              "bypassPermissions": "bypass permissions"}
THUMB_ROWS = 7   # altura das miniaturas das imagens coladas (a última linha é o número)
_sixels = {}   # (arquivo, data, linhas) → (texto sixel, colunas que ocupa)


def sixel(path, rows):
    """Miniatura da imagem em Sixel, com `rows` linhas de altura. Devolve (texto, colunas)."""
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
    """Como fit, para texto com cor: completa com espaço; se passar, tira a cor e corta."""
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
    """Quebra em linhas de até `width` colunas, respeitando as quebras do texto."""
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
    """Tira as marcas de Markdown mais comuns para ler no terminal."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    # Tab e outros caracteres de controle mudam a largura no terminal e empurram a tela.
    text = text.replace("\t", "    ").replace("\r", "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text.replace("```", "")


# Cores do texto da conversa (tema escuro em tons de azul).
def rgb(hex_color, back=False):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"{ESC}{48 if back else 38};2;{r};{g};{b}m"


G_TEXT = rgb("#D9DEE7")
G_HEAD = rgb("#8AB4F8")
G_STRONG = BOLD + rgb("#FFFFFF")
G_EMPH = ESC + "3m" + rgb("#B9C6DA")
G_CODE = rgb("#9CC3FF") + rgb("#1E2633", back=True)
G_BLOCK = rgb("#B9C6DA") + rgb("#1E2633", back=True)
G_LINK = ESC + "4m" + rgb("#8AB4F8")   # sublinhado: dá para clicar
URL_BARE = re.compile(r"(?<![\x01\w/])(https?://[^\s<>`\x01\x02]*[^\s<>`\x01\x02.,;:!?)\]'\"])")
G_HR = rgb("#3A4A63")
CODE = G_CODE


def md_inline(line, state):
    """**negrito**, *itálico*, `código` e links nas cores do tema;
    state = [negrito, código, itálico] passa de uma linha para a outra."""
    line = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", "\x01\\1\x02", line)
    # Link solto (https://…) também em destaque, fora dos trechos de código.
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
    """Texto do Claude em linhas para o terminal: títulos, listas, tabelas e blocos de
    código como a sessão desenha, quebrando no tamanho da coluna."""
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
            # Não cabe: as colunas curtas ficam inteiras e as compridas dividem o resto. O
            # texto da célula quebra em mais linhas, em vez de ser cortado com "…".
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
    ("mcp", "servidores MCP"),
    ("agents", "subagentes"),
    ("hooks", "hooks configurados"),
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

_init_cmds = {}   # (pasta, plugins) → nomes que a própria sessão diz aceitar
_init_running = set()


def _load_init_cmds(key):
    """Pergunta ao Claude, do jeito que a sessão roda (mesma pasta e plugins), a lista de
    comandos dela: o init do -p traz todos (skills, plugins, mods). Para logo depois do init."""
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
    """Nomes dos comandos / da sessão, lidos do próprio Claude (em segundo plano, guardados 10 min)."""
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
    """Os comandos / que a sessão aceita: skills e commands do projeto dela e do usuário."""
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
        # Plugins instalados: os comandos deles têm o nome do plugin na frente (plugin:skill).
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
        # Os comandos do próprio Claude Code (funcionam na sessão em segundo plano).
        for name, desc in BUILTIN_COMMANDS:
            found.setdefault(name, desc)
        return sorted(found.items())
    return cached(("cmds", cwd), 30, load) or []


BANG = re.compile(r"```[^\n]*\n([\s\S]*?)```")


def last_bang(turns):
    """copiar-comando: o último comando "! " depois do seu último pedido, em bloco de
    código, em `crase` ou numa linha solta (o Claude escreve dos três jeitos)."""
    for turn in reversed(turns):
        if turn[0] == "você":
            return None
        if turn[0] == "claude":
            text, found = turn[1], []   # (posição na mensagem, comando)
            fences = [(m.start(), m.end()) for m in BANG.finditer(text)]
            for m in BANG.finditer(text):
                body = m.group(1).strip()
                if body.startswith("! "):
                    found.append((m.start(), " ".join(l.strip() for l in body[2:].splitlines() if l.strip())))
            outside = lambda pos: not any(a <= pos < b for a, b in fences)
            for m in re.finditer(r"`!\s+([^`\n]+)`", text):
                if outside(m.start()):
                    found.append((m.start(), m.group(1).strip()))
            for at, cmd in bang_paragraphs(text):   # parágrafo solto, de uma ou várias linhas
                found.append((at, cmd[2:].strip().rstrip("`")))
            if found:
                return max(found)[1]
    return None


CODE_BG = "48;2;30;38;51"   # o fundo dos trechos de código (G_CODE e G_BLOCK)
COPY_MARK = "🗐"        # copiar (duas folhas): no Windows Terminal ocupa uma coluna só
COPY_BLOCK = COPY_MARK


def code_runs(line):
    """Trechos com fundo de código numa linha já colorida: [(coluna inicial, final, texto)]."""
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
    """Troca as células a partir de col pela marca (1 ou 2 colunas), mantendo as cores."""
    return slice_ansi(line, 0, col) + mark + slice_ansi(line, col + text_width(mark), float("inf"))


def wrap_spans(text, width):
    """Quebra o texto em linhas de até width colunas, de preferência depois de um espaço,
    sem perder nada: devolve [(início, fim)] no texto (as linhas, juntas, são o texto)."""
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
    """Os trechos de código da mensagem original, na ordem em que a tela os desenha:
    blocos ``` inteiros e `inline` (fora de tabela e título, que a tela mostra sem cor)."""
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
    """Põe um 🗐 no fim de cada trecho azul (código inline e bloco de código) e guarda o
    texto dele em buttons, para o clique copiar. O 🗐 entra no lugar do espaço que fecha
    o trecho (ou da última célula do bloco): a linha não muda de largura. Com sources (o
    texto original), copia o trecho inteiro, mesmo o que a tela cortou ou quebrou."""
    found = []   # ([(linha, coluna inicial, final)], texto da tela): onde o clique copia
    carry, i = "", start
    carry_at = []
    while i < len(convo):
        line = convo[i]
        block = lambda l: l[2:].startswith(G_BLOCK)   # a linha vem com "● " ou "  " na frente
        if block(line):   # bloco de código: um botão para o bloco inteiro
            j = i
            while j < len(convo) and block(convo[j]):
                j += 1
            text = "\n".join(ANSI.sub("", l)[4:].rstrip() for l in convo[i:j]).strip("\n")
            width = visible_width(line)
            if width > 5:
                convo[i] = _put(line, width - 3, COPY_BLOCK)
                found.append(([(k, 2, width - 1) for k in range(i, j)], text))   # o bloco todo é clicável
            i = j
            continue
        runs = code_runs(line)
        for n, (a, b, text) in enumerate(runs):
            closed = text.endswith(" ") or n < len(runs) - 1   # o espaço do fim fecha o trecho
            at = [(i, a, b)]
            if n == 0 and carry and a <= 4:
                text = carry + " " + text.strip()   # o trecho começou na linha de cima
                at = carry_at + at
            carry, carry_at = "", []
            if closed:
                line = _put(line, b, COPY_MARK)
                found.append((at, text.strip()))
            else:
                carry, carry_at = text.strip(), at   # quebrou no fim da linha: continua na de baixo
        convo[i] = line
        i += 1
    # Mesma quantidade de trechos na tela e no original: copia o original (inteiro).
    exact = sources if sources is not None and len(sources) == len(found) else None
    for k, (spots, text) in enumerate(found):
        body = exact[k] if exact else text
        if body.strip():
            for line_no, a, b in spots:   # o trecho azul inteiro copia, não só o ícone
                buttons.setdefault(line_no, []).append((a, b, ("copy", body)))


def bang_paragraphs(text):
    """Comandos "! …" escritos como parágrafo solto (fora de bloco de código): do "! " até
    a linha em branco. Comando de várias linhas (ssh com um script python dentro) vem
    inteiro, com as quebras: copiar só a 1ª linha deixava o comando pela metade.
    Devolve [(posição na mensagem, comando)]."""
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
    """Clique num link da mensagem abre no navegador. [texto](url) aparece só o texto: o
    clique nele abre a url. Link solto quebrado em duas linhas: a 1ª parte abre o link inteiro."""
    links = [(m.group(1), m.group(2)) for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)]+)\)", text or "")]
    # Link dentro de `código` ou de bloco ``` fica com o 🗐 (copiar), não abre.
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
    """Comando "! …" que o Claude escreve como parágrafo (sem crase): a tela mostra sem
    fundo de código, então ele não ganha 🗐. Aqui ganha: o parágrafo inteiro copia o
    comando original, numa linha só, mesmo quebrado em várias linhas na tela."""
    commands = [cmd for _, cmd in bang_paragraphs(text)]
    i = start
    for cmd in commands:
        head = " ".join(cmd.split())[:20]
        while i < len(convo) and not " ".join(ANSI.sub("", convo[i])[2:].split()).startswith(head):
            i += 1
        if i >= len(convo):
            return
        j = i + 1   # o parágrafo vai até a linha em branco
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
    env = dict(os.environ, AGENTES_CLIP=text)
    return subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $env:AGENTES_CLIP"],
                          env=env, capture_output=True, timeout=10, creationflags=0x08000000).returncode == 0


def copy_file(path):
    """Põe o próprio arquivo na área de transferência (como o ctrl+c no Explorer): o ctrl+v no
    Teams, no WhatsApp ou no e-mail anexa o arquivo. O caminho vai pela variável de ambiente."""
    # O "Copiar" do próprio Windows (clique direito → Copiar): só ele põe tudo o que o Teams e o
    # WhatsApp leem (Shell IDList Array, FileGroupDescriptorW…); só a lista de arquivos não colava.
    # O OleFlushClipboard deixa a cópia valendo depois que o powershell termina.
    script = ("Add-Type -AssemblyName System.Windows.Forms;"
              "Add-Type -Namespace W -Name Ole -MemberDefinition "
              "'[DllImport(\"ole32.dll\")] public static extern int OleFlushClipboard();';"
              "$p = $env:AGENTES_CLIP;"
              "$i = (New-Object -ComObject Shell.Application).Namespace((Split-Path $p)).ParseName((Split-Path $p -Leaf));"
              "if (-not $i) { exit 2 };"
              "$i.InvokeVerb('copy');"
              "$t = (Get-Date).AddMilliseconds(1500);"
              "while ((Get-Date) -lt $t) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 };"
              "[void][W.Ole]::OleFlushClipboard();"
              "if (-not [System.Windows.Forms.Clipboard]::ContainsFileDropList()) { exit 3 }")
    env = dict(os.environ, AGENTES_CLIP=path)
    return subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                          env=env, capture_output=True, timeout=20, creationflags=0x08000000).returncode == 0


TOOL_KEYS = ("command", "file_path", "notebook_path", "pattern", "url", "query", "skill", "description",
             "path", "action", "prompt")


def tool_line(name, data, result, err, width):
    """tool-lines: a ferramenta numa linha só — tool_call: Nome(assunto) - nota —, sem a saída."""
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
        self.cursor = 0          # posição na lista de itens (títulos + linhas)
        self.collapsed = set()   # grupos recolhidos
        self.prompt = ""         # texto da caixa de baixo
        self.mode = "task"       # task | reply | find
        self.reply_to = None
        self.find = ""
        self.help = False
        self.compose_new = False   # Kanban: ctrl+n faz o texto virar sessão nova, e não resposta
        self.help_button = (0, 0)
        # Foco: sessão escolhida e o que você já viu de cada sessão ({id: ms}).
        self.fid = None
        self.hover = None          # Kanban: (id da sessão, área do card) sob o mouse
        # Kanban: pop-up do card escolhido, ligado por padrão (ctrl+o liga/desliga e fica salvo).
        self.peek = read_json(CONFIG_FILE, {}).get("peek", True)
        self.popup_lock = None     # Kanban: pop-up preso na sessão enquanto você responde por ele
        self.cmd_sel = 0           # comando / escolhido no menu
        self.tools_open = False    # ctrl+e: mostra a saída das ferramentas (o ▸ do tool-lines)
        self.copied = None         # último "! comando" copiado com ctrl+y
        self.pscroll = 0           # rolagem da resposta no pop-up / Foco (pgup/pgdn)
        # ctrl+u: índice dos seus prompts (como os tracinhos do linha-status).
        self.sel = None            # trecho da conversa selecionado com o mouse: (sessão, início, fim)
        self.sel_drag = False
        self.index_open = False
        self.index_sel = None      # número do prompt escolhido no índice (1 = o primeiro)
        self.index_jump = None     # prompt para onde a conversa rola no próximo desenho
        self.turn_limit = (None, 300)   # (sessão, itens da conversa carregados); cresce ao pular para um prompt antigo
        self.seen = read_json(CONFIG_FILE, {}).get("seen", {})
        # Nomes novos (ctrl+r) que ainda não chegaram na sessão: aparecem na hora e
        # saem daqui quando o Claude Code confirma o nome.
        self.names = read_json(CONFIG_FILE, {}).get("names", {})
        self.outbox = {}   # id → mensagens suas que ainda não apareceram na conversa
        # Fila (como no Codex): tab guarda a mensagem; ela vai sozinha quando a sessão
        # termina a vez dela, uma de cada vez. Fica no disco: o F5 não perde a fila.
        saved = read_json(QUEUE_FILE, {})
        self.waitlist = {k: v for k, v in saved.items() if isinstance(v, list) and v} if isinstance(saved, dict) else {}
        self.dispatched = {}   # id → (hora do envio, turn_start de quando mandou)
        # id → quando ela passou a esperar você (pisca até você olhar); sobrevive ao F5.
        self.alerts = read_json(CONFIG_FILE, {}).get("alerts", {})
        self.features = read_json(CONFIG_FILE, {}).get("features", {})   # {chave: False} = desligada
        self.feat_open = False
        self.feat_button = (0, 0)
        self.feat_hits = []   # (linha, coluna inicial, final, chave) de cada chave do painel
        self.find_hits, self.hits_for, self.searching = {}, "", False   # busca dentro das conversas
        self.waiting_button = (0, 0, 0)   # linha e colunas do botão "esperando você"
        self.rang = 0.0
        self.notice = ""
        self.notice_until = 0    # até quando o aviso resiste às teclas
        self.confirm = None      # item esperando o segundo ctrl+x
        self.tick = 0
        self.queue = []          # teclas já lidas de uma sequência VT
        self.button = (0, 0)     # colunas do botão Atualizar na linha 1
        self.view_button = (0, 0)  # colunas do botão Kanban/Lista na linha 1
        self.reload = False
        self.input_mode = None
        # Kanban (Ctrl+S troca a visão): coluna e card escolhidos, primeira coluna
        # visível, rolagem de cada coluna, áreas clicáveis e o card sendo arrastado.
        self.view = "split"   # só a visão Lista + conversa (as outras saíram da tela)
        self.kcol, self.krow, self.kstart = 0, 0, 0
        self.kscroll = {}
        self.hits = []
        self.drag = None
        self.gsel = 0            # painel escolhido na visão Painéis
        self.ghits = []
        self.bcol, self.brow = 0, 0   # tarefa escolhida na visão Projetos
        self.bscroll = {}
        self.bhits = []
        self.bgoal = 0                # meta escolhida (0 = todas)
        self.bside = False            # foco na coluna das metas
        self.shits = []
        threading.Thread(target=self.poll, daemon=True).start()

    # ------------------------------------------------ dados

    def poll(self):
        # A cada 3 s, ou na hora em que o estado de alguma sessão muda (olhar a data dos
        # state.json é barato); depois da primeira volta, pré-carrega as conversas.
        warmed = False
        while True:
            self.refresh()
            _safe(self.dispatch_waitlist)
            if not warmed:
                warmed = True
                self.background(self.prewarm_all)
            seen = self.state_stamps()
            time.sleep(0.5)   # sessão escrevendo sem parar não vira leitura sem fim
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
        """Teclas com a fila aberta: ↑↓ escolhe, enter edita, shift+↑↓ (ou alt+↑↓) muda a
        ordem, del/ctrl+x/backspace cancela, esc fecha."""
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
        elif ch == "\x0b":   # ctrl+k: esvazia a fila desta sessão
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
        elif ch == "s":   # steer: manda já, no meio do trabalho
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
        """Um botão da fila: steer manda já (a sessão lê no meio do trabalho, como o Enter),
        edit leva o texto para a caixa, cancel tira da fila."""
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
        """Editando uma da fila: enter/tab grava o texto no lugar dela (não manda)."""
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
        """Manda a próxima da fila de cada sessão que terminou a vez dela. Depois de mandar,
        espera a sessão começar (e terminar) o turno novo antes da seguinte."""
        if not self.on("auto_queue"):   # desligada no painel de features: a fila espera você
            return
        for sid, items in list(self.waitlist.items()):
            row = next((r for r in self.rows if r["id"] == sid), None)
            if not items or row is None:
                continue
            if (getattr(self, "qedit", None) or {}).get("id") == sid or (getattr(self, "qediting", None) or ("",))[0] == sid:
                continue   # você está mexendo na fila dela: espera
            if row["busy"] or row.get("questions") or row.get("permission"):
                continue
            if len(items) > QUEUE_AUTO_MAX and self.on("queue_pause"):
                # Fila grande demais para ser de propósito: não manda sozinho, espera você olhar.
                self.notice, self.notice_until = (f"queue for {row['name']} paused: {len(items)} messages · "
                                                  "alt+↑ to review · ctrl+k clears it"), time.time() + 6
                continue
            if any(m["state"] == "enviando" for m in self.outbox.get(sid, [])):
                continue
            _safe(live_turns, row.get("transcript"), 1)   # atualiza o turno dela (leitura incremental)
            start = (_meta.get(row.get("transcript")) or {}).get("turn_start")
            sent_at, start_then = self.dispatched.get(sid, (0, None))
            if sent_at and start == start_then and time.time() - sent_at < 90:
                continue   # a anterior ainda não virou turno na conversa
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
        """Lê por trás a conversa de cada sessão, para trocar de uma para outra na hora."""
        for _ in range(40):   # espera o primeiro desenho dizer a largura da conversa
            if getattr(self, "pane_inner", None):
                break
            time.sleep(0.1)
        with self.lock:
            rows = list(self.rows)
            item = self.current()
        # Primeiro a que está na tela, depois as que esperam você e as que trabalham.
        here = item[1]["id"] if item and item[0] == "row" else None
        rows.sort(key=lambda r: (r["id"] != here, r["group"] != "needs", not r["busy"]))
        for r in rows:
            path = r.get("transcript")
            if path:
                turns = _safe(live_turns, path, 300) or []
                _safe(all_prompts, path)
                # Nas larguras da conversa ao lado: a do último desenho e as da Lista +
                # conversa e do Kanban (mesma conta do draw).
                cols = max(80, shutil.get_terminal_size((160, 45))[0] - 2)
                widths = {getattr(self, "pane_inner", None), cols - 60 - 1 - 4,
                          cols - max(64, cols * 55 // 100) - 1 - 4} - {None}
                for inner in widths:
                    for t in turns:
                        if t[0] == "claude":
                            _safe(md_lines, t[1], inner - 2)
                        elif t[0] == "você":
                            _safe(wrap, prompt_text(t[1]), inner - 15)
                time.sleep(0.05)   # deixa a tela respirar entre uma e outra

    def refresh(self):
        try:
            rows = snapshot()
        except Exception as exc:
            self.notice = f"could not list sessions: {exc}"
            return
        # A lista se reordena sozinha (sessão que muda de estado sobe ou desce): a
        # escolha acompanha a sessão em que você está, e não a posição na tela.
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
                # A sessão voltou o nome antigo (gravou o estado por cima): grava de novo.
                r["name"] = wanted
                self.rename_session(r, wanted)
        if self.loaded:
            # Terminou ou parou numa pergunta: pisca na lista e toca um som.
            # Só vale se ela continuar parada por 6 s: entre um passo e outro a sessão
            # aparece parada por um instante e o aviso piscava à toa.
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
            # Primeira vez: o que já existe conta como visto; a novidade vale daqui em diante.
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
        """Grupos criados por você (Ctrl+G): {"order": [nomes], "sessions": {id: nome}}.
        Ficam só nesta réplica: no original o recurso de grupos está desligado."""
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
        """Troca o nome de um grupo seu, no mesmo lugar da ordem e com as mesmas sessões."""
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
        """Renomeia na hora, como o ctrl+r do claude agents: grava o nome no estado da
        sessão (o claude agents lê dali) e no arquivo da conversa (o /resume lê dali).
        Não manda mensagem nem para a sessão, mesmo trabalhando."""
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
        """Nome do grupo seu do item escolhido (título do grupo ou sessão dentro dele)."""
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
        """Shift+↑ / Shift+↓: sobe ou desce o grupo seu (no Kanban, a coluna vai para
        a esquerda ou a direita). A escolha continua no mesmo grupo."""
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
        """Leva o grupo inteiro um lugar para cima (ou para a esquerda, no Kanban)."""
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

    # ------------------------------------------------ painéis (estilo Agents Window do Cursor)

    def flat_rows(self):
        """Sessões na mesma ordem da lista (fixadas, seus grupos, estados)."""
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
        """Um painel de largura w e altura h (borda inclusa)."""
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
        area_w = cols - side_w - 2   # "│ " separa a lista dos painéis
        tcols = 2 if area_w >= 90 else 1
        trows = 2 if room >= 30 else 1
        per_page = tcols * trows
        tile_w = (area_w - (tcols - 1)) // tcols
        tile_h = max(10, room // trows)
        page = self.gsel // per_page if rows else 0
        shown = rows[page * per_page:(page + 1) * per_page]
        self.ghits = []

        # Lista da esquerda: 3 linhas por sessão, rolando para manter a escolhida.
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

        # Painéis da direita.
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
        """Colunas do Kanban: as mesmas seções da lista (Pinned, seus grupos, Needs
        input, Working, Completed), na mesma ordem."""
        titles = dict(GROUPS, check=CHECK_TITLE)
        return [(key, key[len("custom:"):] if key.startswith("custom:") else titles.get(key, key), rows)
                for key, rows in self.sections()]

    def popup_target(self):
        """(sessão, área do card) do pop-up: a que você está respondendo por ele, a
        que está sob o mouse, ou a escolhida com ctrl+o."""
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
        """Lado direito do Kanban: a conversa da sessão escolhida (seus pedidos e as
        respostas do Claude), a pergunta pendente e a caixa de responder."""
        inner = width - 4
        self.pane_inner = inner
        if not row:
            body = [DIM + "pick a card with the arrow keys" + RESET]
            top, convo, foot, every, marks, buttons = body, [], [], [], [], {}
        else:
            chip, _ = self.chip(row)
            where = os.path.basename(row.get("cwd") or "")
            # Duas linhas: nome, estado e dados numa só; embaixo, o resumo de onde a sessão está.
            turns = live_turns(row.get("transcript"), limit=300)
            extras = []
            local = localhost_of(row.get("cwd") or "") if self.on("localhost") else None
            if local:
                extras.append(GREEN + "▶ " + local + RESET)
            elif local == "":
                extras.append(DIM + "▶ localhost stopped" + RESET)
            arts = session_artifacts(turns) if self.on("artifacts") else []
            if arts:   # clicável: abre a lista deles no lugar da conversa
                extras.append(ESC + "48;2;45;55;72m" + f" ⧉ {len(arts)} artifact{'s' if len(arts) > 1 else ''} " + RESET)
            extras.append(ESC + "48;2;45;55;72m" + " ☰ index " + RESET)   # clicável, igual ao ctrl+u
            # Sem nome e estado: já estão na lista, ao lado. Começa pelo modo.
            mode = self.mode_of(row) if self.on("mode") else ""
            head = "   ".join(p for p in (mode, "  ".join(extras)) if p)
            # Logo depois do ☰ index: a pasta em que a sessão está aberta, o branch e quanto
            # a sessão custaria pela API. Sem espaço, o caminho encurta pelo começo (…\GitHub\modsv2).
            cost = f"  💲 {cost_label(row)} API" if self.on("cost") and cost_label(row) else ""
            if row.get("cwd") and self.on("folder"):
                branch = f"  🌿 {row['branch']}" if row.get("branch") else ""
                parts = re.split(r"[\\/]", row["cwd"])
                # Sem espaço, encolhe o caminho e depois tira o branch: o custo fica sempre.
                for where in (f"📂 {row['cwd']}{branch}{cost}",
                              "📂 …\\" + "\\".join(parts[-2:]) + branch + cost,
                              "📂 " + parts[-1] + branch + cost, "📂 " + parts[-1] + cost,
                              cost.strip()):
                    if visible_width(head) + 3 + text_width(where) <= inner:
                        break
                head += "   " + DIM + where + RESET
            elif cost:
                head += "   " + DIM + cost.strip() + RESET
            # A linha de estado (nome, modo, localhost, ☰ index, pasta, custo) vai no pé do
            # quadro, como a linha de status embaixo da sessão; no alto fica só o resumo.
            state_line = fit_ansi(head, inner)
            top = []
            resumo = (session_brief(row, turns) or row.get("summary") or row.get("intent") or "") if self.on("summary") else ""
            resumo = re.sub(r"(?i)(bearer\s+|token[=:]\s*)\S+", r"\1•••", resumo)
            if resumo:
                for n, l in enumerate(wrap(resumo, inner - 8)[:2]):
                    top.append((ORANGE + BOLD + "SUMMARY " + RESET if n == 0 else " " * 8) + l)
                top.append(DIM + "─" * inner + RESET)
            # Conversa ao vivo: lida do arquivo que a sessão grava enquanto trabalha
            # (texto do Claude, ferramentas que ele usa e seus pedidos).
            if self.pscroll == 0 and self.index_jump is None and _window.pop(row.get("transcript"), None) is not None:
                self.turn_limit = (None, 300)   # voltou ao fim: a conversa volta a ser a ao vivo
            limit = self.turn_limit[1] if self.turn_limit[0] == row["id"] else 300
            turns = live_turns(row.get("transcript"), limit=limit)
            every = all_prompts(row.get("transcript"))
            number = len(every) - sum(1 for t in turns if t[0] == "você")   # prompts antes do trecho carregado
            # Até este número, o prompt é de antes dos 8 MB que a conversa ao vivo lê.
            marks = []   # (linha da conversa, número do prompt)
            buttons = {}   # linha da conversa → [(coluna inicial, final, arquivo)]
            convo = []
            for turn in turns:
                who = turn[0]
                if who == "você":
                    convo.append("")
                    number += 1
                    marks.append((len(convo), number))
                    # Data e hora em que você mandou, no canto direito da 1ª linha do balão.
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
                        # Aberta na sessão: ela aparece inteira embaixo (NEEDS YOU), não aqui também.
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
                    # Ferramentas seguidas ficam coladas, sem linha em branco entre elas (tool-lines).
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
                    # 🗐 no fim de cada trecho azul; copia o original, inteiro.
                    if self.on("code_copy"):
                        add_copy_buttons(convo, buttons, first_line, code_sources(turn[1]))
                    if self.on("bang_buttons"):
                        add_bang_buttons(convo, buttons, first_line, turn[1], inner)
                    if self.on("link_buttons"):
                        add_link_buttons(convo, buttons, first_line, turn[1])
                    # No fim da mensagem, um botão para cada arquivo ou imagem que ela cita.
                    line, x = "  ", 2
                    files = message_files(turn[1], VIEW_EXT) if self.on("file_buttons") else []
                    if files:
                        convo.append("")   # respiro entre o texto e os botões
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
                            convo += [line, ""]   # uma linha livre entre as fileiras de botões
                            line, x = "  ", 2
                        buttons.setdefault(len(convo), []).append((x, x + w - 1, path))
                        color = (ESC + "48;2;30;90;50m" + ESC + "38;2;200;255;210m") if clicked else (ESC + "48;2;45;55;72m" + ESC + "38;2;156;195;255m")
                        line += color + label + RESET
                        x += w
                        if self.on("file_copy") and os.path.isfile(path):
                            # Colado ao botão do arquivo: copia o arquivo em si, para o ctrl+v no Teams.
                            copied = getattr(self, "file_copied", None)
                            done = copied and copied[0] == path and time.time() - copied[1] < 4
                            tag = " ✓ copied " if done else " ⧉ copy file "
                            buttons[len(convo)].append((x, x + text_width(tag) - 1, ("file", path)))
                            line += ESC + "48;2;35;42;56m" + ESC + ("38;2;200;255;210m" if done else "38;2;170;180;200m") + tag + RESET
                            x += text_width(tag)
                            # Plano B, que funciona em qualquer app: abre a pasta com o arquivo já
                            # selecionado; dali, arrastar para a conversa ou ctrl+c / ctrl+v.
                            show = " ⌕ in folder "
                            buttons[len(convo)].append((x, x + text_width(show) - 1, ("show", path)))
                            line += ESC + "48;2;35;42;56m" + ESC + "38;2;170;180;200m" + show + RESET
                            x += text_width(show)
                        line += "  "
                        x += 2
                    if x > 2:
                        convo.append(line)
            convo = [l.replace("\x00", "") for l in convo]
            # Suas mensagens ainda não gravadas pela sessão: aparecem já, com o estado do envio.
            # "!ssh x" que você digita volta gravado como "! ssh x": compara sem o ! e os espaços.
            same = lambda text: " ".join(prompt_text(text).split()).lstrip("! ")
            said = {same(t[1]) for t in turns[-40:] if t[0] == "você"}
            # Some quando aparece na conversa. A que não foi fica uns minutos com o motivo e
            # depois sai: presa no fim, parecia que a conversa tinha parado de atualizar.
            box = [m for m in self.outbox.get(row["id"], [])
                   if (m["state"] == "fechada" or same(m["text"]) not in said)
                   and time.time() - m["t"] < (180 if m["state"] == "erro" else 1800)]
            self.outbox.setdefault(row["id"], [])[:] = box   # o draw já segura a trava
            # "! comando" só entra na conversa quando termina: até lá, com a sessão ocupada, ele
            # está rodando, e o que você mandou depois espera na fila da sessão.
            ahead = False
            for m in box:
                arrived = same(m["text"]) in said
                if not arrived:   # o /exit que já está na conversa fica só com o aviso embaixo
                    convo.append("")
                for n, l in enumerate([] if arrived else wrap(prompt_text(m["text"]), inner - 2)):
                    convo.append(ESC + "48;2;55;55;55m" + ("> " if n == 0 else "  ") + l
                                 + " " * max(0, inner - 2 - visible_width(l)) + RESET)
                state = {"enviando": DIM + "  sending…", "ok": GREEN + "  ✓ sent",
                         "incerto": YELLOW + "  ? typed, not confirmed", "erro": RED + "  ✗ not sent",
                         "fechada": DIM + "  • session closed · your next message reopens it"}[m["state"]]
                if m["state"] != "ok" and m.get("why"):   # o motivo fica junto, não só no aviso que some
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
            status = self.turn_line(row, _meta.get(row.get("transcript")) or {}) if self.on("turn_line") else ""
            if status:
                convo += ["", status]
                # Cada subagente e comando que roda por trás, com o nome e há quanto tempo.
                if row.get("background") and self.on("background"):
                    convo += self.background_lines(row, turns, inner)
            foot = []
            dialog = self.dialog_of(row)
            if dialog and getattr(self, "panel", None) and self.panel["id"] == row["id"]:
                # Menu de um comando / (/model, /config…): as teclas vão para ele.
                foot.append(ORANGE + BOLD + "MENU · ↑↓ ←→ enter, letters and numbers go to the session · esc closes" + RESET)
                foot += ["  " + fit(l, inner - 2) for l in dialog[-16:]]
            elif dialog:
                # A pergunta/permissão como está na sessão: o número que você aperta vai direto para ela.
                asks = row.get("questions") or []
                # A sessão mostra uma pergunta por vez; as abas ("☒ Cor ☐ Cartão") dizem qual é a da vez.
                tabs = next((l for l in dialog if l.lstrip().startswith("←")), "")
                now = min(len(asks), tabs.count("☒") + 1) if asks else 0
                count = f" · question {now} of {len(asks)}" if len(asks) > 1 else ""
                multi = any(re.match(r"\s*(?:❯\s*)?\d+\.\s*\[.\]", l) for l in dialog)
                foot.append(YELLOW + BOLD + "NEEDS YOU" + count + RESET + DIM + (
                    " · pick several: type the numbers (e.g. 1 3) and enter · or type your own answer" if multi
                    else " · press the option number · or type your own answer and press enter" if asks
                    else " · press the option number right here") + RESET)
                foot += [x for x in (dialog_line(l, inner - 2) for l in dialog[-28:]) if x]
                for k, q in enumerate(asks[now:] if self.on("next_questions") else [], now + 1):   # as que vêm depois desta
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
                # copiar-comando: o último "! comando" que o Claude mandou, pronto para colar.
                done = getattr(self, "copied", None) == bang
                foot.append((GREEN + "Copied ✓" if done else ORANGE + BOLD + "ctrl+y copies") + RESET
                            + DIM + " ! " + fit(bang, inner - 18).rstrip() + RESET)
            # A caixa de digitar fica só embaixo da tela (a mesma da lista): aqui não se repete.
            if foot:
                foot.insert(0, "")   # linha livre entre a conversa e o rodapé (ctrl+y, pergunta…)
            ctx = context_lines(row, inner) if self.on("ctx_bar") else []   # a barra do /context, acima da linha de estado
            foot += [DIM + "─" * inner + RESET] + ctx + [state_line]
        area = max(1, height - 2 - len(top) - len(foot))
        while convo and not convo[-1]:
            convo.pop()
        self.pane_area = area
        self.index_total = len(every)
        if self.index_jump is not None:
            # Enter no índice: o prompt escolhido vai para o alto da conversa.
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
        art_rows = {}   # linha da lista de artifacts → link
        found = self.hits_of(row)
        if row and getattr(self, "arts_open", None) == row["id"]:
            # Clique no ⧉: os artifacts da sessão, um por linha; clique abre no navegador.
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
        # Barra de rolagem na borda direita da conversa: o trecho claro é o que está na tela,
        # e cada ┤ é um prompt seu (o mesmo índice dos tracinhos do linha-status).
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
        # Onde o clique pega (coluna e linha da tela, contando de 1): o botão ☰ índice,
        # cada linha do índice e cada ┤ da barra levam até o prompt.
        x0, y0 = getattr(self, "pane_at", (0, 0))
        hits = []
        if row and foot:
            # O ☰ index está na última linha do quadro (a de estado).
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
            if at >= 0 and row.get("cwd"):   # clique na pasta copia o caminho inteiro dela
                x = x0 + 2 + text_width(plain[:at])
                w = text_width(plain[at:].split("  ")[0])
                # Folga de 2 colunas: o terminal pode desenhar um ícone mais largo que a conta.
                hits.append((x - 2, x + w + 2, y0 + 1 + last, "copy", row["cwd"]))
        for i, n in index_rows.items():
            hits.append((x0 + 2, x0 + 1 + inner, y0 + 1 + len(top) + i, "go", n))
        for i, url in art_rows.items():
            hits.append((x0 + 2, x0 + 1 + inner, y0 + 1 + len(top) + i, "open", url))
        if not index_rows and not art_rows:
            for i in range(first, end):
                for a, b, path in buttons.get(i, []):
                    # ("copy", texto): o 🗐 dos trechos azuis · ("file", caminho): copiar o arquivo
                    kind = ({"file": "copyfile", "show": "showfile"}.get(path[0], "copy")
                            if isinstance(path, tuple) else "open")
                    hits.append((x0 + 2 + a, x0 + 2 + b, y0 + 1 + len(top) + i - first,
                                 kind, path[1] if isinstance(path, tuple) else path))
        ticks = []   # os ┤ da barra: o mouse em cima mostra o prompt
        if total > area:
            for line, n in (marks if self.on("scroll_ticks") else []):
                i = min(area - 1, line * area // total)
                hits.append((x0 + width - 2, x0 + width, y0 + 1 + len(top) + i, "go", n))
                ticks.append((x0 + width - 2, x0 + width, y0 + 1 + len(top) + i, n))
        self.pane_hits = (row, hits)
        self.tick_hits = (row, ticks)
        # Onde fica o texto da conversa na tela, para selecionar arrastando o mouse.
        self.pane_geo = (row["id"], x0, y0 + 1 + len(top), first, end, inner, convo) if row and not index_rows else None
        return box

    def select_mouse(self, kind, x, y):
        """Selecionar texto da conversa: aperta, arrasta e solta. Ao soltar, o trecho vai
        para a área de transferência (ctrl+v cola). Arrastar além do topo ou do pé rola.
        Devolve None quando o mouse não é com a seleção."""
        geo = getattr(self, "pane_geo", None)
        if kind == "down":
            self.sel_drag = False
            if not geo:
                return None
            rid, x0, top, first, end, inner, _ = geo
            hits = getattr(self, "pane_hits", (None, []))[1]
            if not (x0 + 2 <= x <= x0 + 1 + inner and top <= y < top + (end - first)) or any(
                    h[0] <= x <= h[1] and h[2] == y for h in hits):
                self.sel = None   # clique fora (ou num botão) desfaz a seleção
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
        # Soltou: copia o trecho (se arrastou; um clique só não copia nada).
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
            if c0 == 0 and piece[:2] in ("● ", "  ", "> "):   # tira o recuo da conversa
                piece = piece[2:]
            # O 🗐 dos trechos de código é da tela, não do texto: não vai junto na cópia.
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
        """Esc numa sessão trabalhando: o primeiro avisa, o segundo (em até 4 s) para o
        trabalho com `claude stop`. A conversa fica guardada: a próxima mensagem continua."""
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
        """Esc (ou ctrl+c) sem nada para cancelar: pergunta antes de fechar. O segundo
        esc em até 4 segundos fecha; qualquer outra tecla fica."""
        if time.time() - getattr(self, "quitting", 0) < 4:
            return False
        self.quitting = time.time()
        self.notice, self.notice_until = "⚠ quit the program? esc again quits · any other key stays", time.time() + 4
        return True

    def open_file(self, path):
        """Botão de arquivo: o botão fica verde ("abrindo…") na hora e o Windows abre o
        arquivo em segundo plano, sem travar a tela. Cliques repetidos no mesmo arquivo
        enquanto ele abre não abrem de novo."""
        name = os.path.basename(path.rstrip("\\/")) or path
        busy = getattr(self, "opening", None)
        if busy and busy[0] == path and time.time() - busy[1] < 4:
            self.notice, self.notice_until = f"already opening {name}…", time.time() + 3
            return
        self.opening = (path, time.time())
        self.notice, self.notice_until = f"opening {name}…", time.time() + 4

        def run():
            try:
                # .md abre formatado no navegador, não como texto cru no editor.
                md = path.lower().endswith((".md", ".markdown")) and os.path.isfile(path) and self.on("md_browser")
                open_detached(md_page(path) if md else path)
            except OSError:
                self.notice, self.notice_until = f"could not open {path}", time.time() + 5
                self.opening = None
        threading.Thread(target=run, daemon=True).start()

    def prompt_index(self, every, marks, first, end, inner, area):
        """ctrl+u: todos os seus prompts da sessão, numerados, no lugar da conversa.
        Os que estão na tela agora ficam em laranja; enter rola a conversa até o escolhido."""
        total = len(every)
        self.index_sel = max(1, min(total, self.index_sel or total))
        on_screen = {n for line, n in marks if first <= line < end}
        lines = [ORANGE + BOLD + f"INDEX · {total} prompt{'s' if total > 1 else ''}" + RESET
                 + DIM + "  ↑↓ pick · enter jumps there · esc closes" + RESET, ""]
        rows = area - len(lines)
        start = max(0, min(self.index_sel - 1 - rows // 2, total - rows))
        num_w = len(str(total))
        where = {}   # linha da lista → número do prompt (para o clique)
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
        """Enter no índice: carrega a conversa desde o prompt escolhido, se ele for antigo,
        e pede para o próximo desenho rolar até ele."""
        path = row.get("transcript")
        total = len(all_prompts(path))
        if not self.index_sel or self.index_sel > total:
            return
        full = live_turns(path, limit=10 ** 9)
        spots = [i for i, t in enumerate(full) if t[0] == "você"]
        floor = total - len(spots)
        if self.index_sel <= floor:
            # Prompt de antes do trecho lido: passa a ler a conversa desde ele.
            offset = prompt_offset(path, self.index_sel)
            if offset is None:
                return
            _window[path] = max(0, offset - 1)
            full = live_turns(path, limit=10 ** 9)
            spots = [i for i, t in enumerate(full) if t[0] == "você"]
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
        """Com o pop-up aberto, o que você digita vai para a sessão dele (e ele fica
        preso nela até enviar); sem pop-up, vai para o card escolhido."""
        target = self.popup_target() if self.view == "kanban" else None
        if target:
            self.popup_lock = (target[0]["id"], target[1])
            return target[0]
        self.popup_lock = None
        return fallback

    def kanban_popup(self, lines, cols):
        """Pop-up do "onde paramos" ao lado do card sob o mouse (ou do escolhido, com ctrl+o)."""
        target = self.popup_target()
        if not target:
            return lines
        row, (x0, x1, y0, _) = target
        pw = min(76, cols - 4)
        ph = min(len(lines) - 6, 24)
        typing = self.prompt if self.mode == "reply" and self.reply_to and self.reply_to["id"] == row["id"] else None
        box = self.focus_box(row, pw, ph, edge=ORANGE, compact=True, typing=typing if typing is not None else "")
        bw = pw + 1   # focus_box começa com um espaço
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
        # Pinned fixa; um grupo seu põe no grupo; uma coluna de estado tira do
        # grupo e da fixação, e a sessão cai na coluna do estado dela.
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
        """Selo do estado do card: o que a sessão espera de você."""
        if row["busy"]:
            return "\x1b[38;2;90;150;235m◌ working" + RESET, 13
        if row["group"] == "needs":
            return YELLOW + BOLD + "● needs you" + RESET, 17
        return GREEN + "✓ done" + RESET, 10

    def cmd_menu(self):
        """Comandos / que casam com o que você digitou (só no começo da resposta)."""
        text = self.prompt
        if self.mode != "reply" or not self.reply_to or not text.startswith("/") or " " in text or "\n" in text:
            return []
        typed = text[1:].lower()
        names = slash_commands(self.reply_to.get("cwd"))
        # Mais os que a própria sessão diz aceitar (mods, plugins, skills de fora), sem descrição.
        known = {n for n, _ in names}
        names = sorted(names + [(n, "") for n in session_commands(self.reply_to) if n not in known])
        hits = [c for c in names if c[0].lower().startswith(typed)] + \
               [c for c in names if typed and typed in c[0].lower() and not c[0].lower().startswith(typed)]
        if typed != getattr(self, "cmd_typed", None):
            self.cmd_typed, self.cmd_sel = typed, 0
        return hits

    def with_images(self, text):
        """Troca cada [Imagem #n] pelo caminho do arquivo, para a sessão abrir a imagem."""
        images, self.images = getattr(self, "images", {}), {}
        return re.sub(r"\[Imag(?:em|e) #(\d+)\]",
                      lambda m: f"[Image #{m.group(1)}: {images[m.group(1)]}]" if m.group(1) in images else m.group(0),
                      text)

    def paste_image(self, item):
        """Alt+V: salva a imagem copiada num arquivo e põe o caminho no texto. A sessão
        recebe o caminho e abre a imagem, como no Alt+V dela."""
        folder = os.path.normpath(os.path.join(HOME, "jobs", "agentes-imagens"))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("imagem-%Y%m%d-%H%M%S.png"))
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
        # Na caixa aparece só [Imagem #1], como na sessão; o caminho entra no envio.
        self.images = dict(getattr(self, "images", {}), **{str(n): path})
        self.prompt += ("" if not self.prompt or self.prompt.endswith(" ") else " ") + f"[Image #{n}] "
        self.notice = f"image {n} pasted"

    def recall(self, code):
        """↑ / ↓ na caixa de responder, como dentro da sessão: com a caixa vazia (ou com um
        prompt que veio daqui, sem mexer), ↑ traz o seu prompt anterior e ↓ o seguinte."""
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
            return True   # já no primeiro: fica
        hist["i"] = min(i, len(hist["list"]))
        text = hist["list"][hist["i"]] if hist["i"] < len(hist["list"]) else ""
        # Imagem colada volta como [Image #1] na caixa; o caminho segue junto no envio.
        self.images = {m.group(1): m.group(0).split(": ", 1)[1][:-1] for m in IMAGE_TAG.finditer(text)}
        self.prompt = prompt_text(text)
        hist["list"][hist["i"]:hist["i"] + 1] = [self.prompt] if text else []
        self.caret = None
        return True

    def answer_by_number(self, row, text):
        """Com perguntas pendentes, "1 2" vira a resposta escrita: uma opção por pergunta."""
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
        """Última parte do que o Claude escreveu: é a pergunta quando ele espera você."""
        if row.get("permission"):
            return "asks permission: " + row["permission"]
        if row.get("questions"):
            asks = row["questions"]
            return asks[0]["question"] + (f" (+{len(asks) - 1} question{'s' if len(asks) > 2 else ''})" if len(asks) > 1 else "")
        text = clean_markdown(row.get("last_text") or "")
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        return " ".join((parts[-1] if parts else "").split())

    def card(self, row, w, selected, dragging):
        """Um card de 7 linhas e largura w (borda inclusa)."""
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
        pad = " "   # fit() já completa o nome até a largura certa
        if self.peek:
            # Com a conversa ao lado, o card fica só com o principal: nome, estado,
            # uma linha do que está acontecendo e o tempo.
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
        """Linhas do quadro e as áreas clicáveis (self.hits)."""
        columns = self.kanban_columns()
        selected = self.kanban_selected()
        n = len(columns)
        w = max(30, min(70, (cols - (n - 1)) // max(1, n)))
        fits = max(1, (cols + 1) // (w + 1))
        w = max(w, min(70, (cols - (fits - 1)) // fits))   # colunas visíveis ocupam a largura toda
        if self.kcol < self.kstart:
            self.kstart = self.kcol
        if self.kcol >= self.kstart + fits:
            self.kstart = self.kcol - fits + 1
        self.kstart = max(0, min(self.kstart, max(0, n - fits)))
        shown = list(enumerate(columns))[self.kstart:self.kstart + fits]
        card_h = 4 if self.peek else 7   # com a conversa ao lado, cards curtos (cabem mais)
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


    # ------------------------------------------------ projetos (estilo Paperclip)

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
        """Solta um agente (claude --bg) na pasta da tarefa, com a meta e a tarefa no pedido."""
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
        # As colunas do meio seguem os agentes; mover só marca ou desmarca "Feito".
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
        """Coluna da esquerda: metas com o progresso (feitas/total) e o custo."""
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
        w = max(20, (cols - side_w - 5 - (n - 1)) // n)  # 1 + lateral + " │ " + colunas cabe em cols - 1
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


    # ------------------------------------------------ foco (uma sessão por vez, com o "onde paramos")

    def save_config(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"view": self.view, "seen": self.seen, "peek": self.peek, "names": self.names,
                       "alerts": self.alerts, "features": self.features}, f)

    def on(self, key):
        """A feature está ligada? (painel ⊞ Features; tudo ligado por padrão)"""
        return self.features.get(key, True) is not False

    def mark_seen(self, row_id):
        self.seen[row_id] = int(time.time() * 1000)
        self.save_config()

    def unseen(self, row):
        return not row["busy"] and row["updated"] > self.seen.get(row["id"], 0)

    def focus_queue(self):
        """Fila: quem precisa de você (quem espera há mais tempo primeiro), depois as
        que terminaram com novidade, e por último as que estão trabalhando."""
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
        # Escolhe a próxima antes de marcar: a vista sai da fila e mudaria as posições.
        if current and not current["busy"]:
            self.mark_seen(current["id"])

    def focus_events(self, row):
        """O que aconteceu desde a última vez que você olhou (ou os últimos passos)."""
        events = []
        for e in timeline(row["id"]):
            detail = " ".join(str(e.get("detail") or "").split())
            if not detail:
                continue
            if e.get("text"):
                events.append((e["ms"], "claude", detail))
            elif e.get("state") != "working":
                events.append((e["ms"], "você", detail))
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
        # Faixa com a fila: a escolhida em destaque, o resto em volta dela.
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
        """Caixa com o "onde paramos" da sessão: usada no Foco e no pop-up do Kanban.
        compact (pop-up) corta os passos e encurta a resposta; typing mostra a
        caixa de responder dentro dela."""
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
        # Resumo da última tarefa: o que você pediu por último e o que ele fez.
        asked = next((text for _, who, text in reversed(events) if who == "você"), "")
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
                tag = (ESC + "38;2;90;150;235m" + "you   " + RESET) if who == "você" else (DIM + "claude" + RESET)
                body.append(f"{tag} {fit(text, inner - 7)}")
            body.append("")
        needs = row["group"] == "needs" and not row["busy"]
        question = self.question(row) if needs else ""
        reply = clean_markdown(row.get("last_text") or "")
        reply = "\n".join(l for l in reply.split("\n") if l.strip().strip("`") or not l.strip())
        if question:
            # A pergunta já vai embaixo, em destaque: sai do fim da resposta.
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
                # PgUp/PgDn rolam a resposta (self.pscroll = linhas acima do fim).
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
            # Caixa de responder dentro do pop-up: o que você digita aparece aqui.
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

    # ------------------------------------------------ itens da tela

    def sections(self):
        pins = self.pins()
        order, grouped = self.custom()
        rows = self.rows
        if self.find:
            needle = self.find.lower()
            inside = self.find_hits if self.hits_for == self.find else {}
            rows = [r for r in rows if needle in r["name"].lower() or needle in r["summary"].lower() or r["id"] in inside]
        # No fim da lista (o topo é dos seus grupos de backlog), as que terminaram e você ainda
        # não olhou: saem dos outros grupos até você parar nelas, para nenhuma terminar escondida.
        check = [r for r in rows if r["id"] in self.alerts and not r["busy"] and r["group"] != "needs"] \
            if self.on("check_group") else []
        rows = [r for r in rows if r not in check]
        by_id = {r["id"]: r for r in rows}
        out = [("pinned", [by_id[p] for p in pins if p in by_id])]
        # Seus grupos vêm logo depois das fixadas; a sessão sai do grupo de estado.
        for name in order:
            out.append(("custom:" + name, [r for r in rows if grouped.get(r["id"]) == name and r["id"] not in pins]))
        for key, _ in GROUPS[1:]:
            out.append((key, [r for r in rows if r["group"] == key and r["id"] not in pins and r["id"] not in grouped]))
        out.append(("check", check))
        return [(k, items) for k, items in out if items]

    def items(self):
        """Títulos e linhas na ordem da tela: ('head', grupo) ou ('row', linha)."""
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

    # ------------------------------------------------ desenho

    def dialog_of(self, row):
        """A pergunta ou permissão aberta na sessão, lida da tela dela; abre a
        conexão escondida na primeira vez."""
        panel = getattr(self, "panel", None)
        is_panel = bool(row and panel and panel["id"] == row["id"])   # menu aberto por um comando /
        if not row or not row.get("alive") or (not is_panel and (row["busy"] or not (row.get("questions") or row.get("permission")))):
            return []
        conn = _attached.get(row["id"])
        if is_panel and conn and not conn.dead.is_set():
            lines = conn.dialog() if conn.ready.is_set() else []
            if lines:
                panel["seen"] = True
            elif panel["seen"] or time.time() - panel["t"] > 6:   # fechou (ou o comando não tinha menu)
                self.panel = None
            conn.used = time.time()
            return lines
        if conn is not None and not conn.dead.is_set() and not _safe(conn.p.isalive):
            conn.dead.set()   # o terminal escondido fechou: a tela dele é velha, abre outro
        if conn is None or conn.dead.is_set():
            if getattr(self, "warming", None) != row["id"] or conn is not None \
                    or time.time() - getattr(self, "warming_t", 0) > 30:
                self.warming, self.warming_t = row["id"], time.time()
                self.background(lambda r: _safe(attach_open, r), row)
            return []
        conn.used = time.time()
        return conn.dialog() if conn.ready.is_set() else []

    def mode_of(self, row):
        """O modo da sessão, colorido como no Claude. A tela dela vale mais (muda na hora
        com o shift+tab); sem conexão aberta, o último modo gravado na conversa."""
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
        """shift+tab: troca o modo da sessão escolhida, como o shift+tab dentro dela."""
        conn = _safe(attach_open, row)
        if not conn or not conn.ready.wait(15) or conn.dialog() or conn.waiting_you():
            self.notice, self.notice_until = f"{row['name']}: could not switch the mode now", time.time() + 4
            return
        conn.press("\x1b[Z")
        time.sleep(0.5)
        self.notice, self.notice_until = f"{row['name']}: {conn.mode() or 'mode switched'}", time.time() + 4

    def turn_line(self, row, meta):
        """A linha do fim da conversa, no formato do Claude e sempre da mesma conta (a
        conversa gravada), para não mudar de cara ao trocar de sessão:
        trabalhando "✻ Working… (3m 55s · ↓ 3.8k tokens)" · parada "✻ Worked for 16s · done 18:53"."""
        start = stamp(meta.get("turn_start"), 0) / 1000 if meta.get("turn_start") else 0
        # Esperando você numa pergunta ou permissão aberta: o turno não acabou e o pedido
        # já aparece no fim. Parada com uma pergunta no texto ("Quer que eu comece?"), a linha vale.
        if not start or row.get("questions") or row.get("permission"):
            return ""
        tokens = meta.get("turn_tokens") or 0
        tok = f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)
        verbs = ("Working", "Thinking", "Cooking", "Brewing", "Pondering", "Crafting", "Churning", "Baking")
        verb = verbs[int(start) % len(verbs)]   # o mesmo verbo durante todo o turno
        extra = f" · {row['background']} running in background" if row.get("background") else ""
        kinds = [f["kind"] for f in row.get("fan") or []]
        if row.get("background") and kinds:   # "1 agent, 1 shell running in background"
            names = {"agent": ("agent", "agents"), "shell": ("shell", "shells")}
            parts = [f"{kinds.count(k)} {names.get(k, (k, k + 's'))[kinds.count(k) > 1]}" for k in dict.fromkeys(kinds)]
            extra = " · " + ", ".join(parts) + " running in background"
        if row.get("background"):
            # "claude -p" que um script da sessão abriu: o Claude só vê o shell que os abriu.
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
        """O que roda por trás, uma linha cada, como uma lista de tarefas:
        "  ✶ Run full test suite and commit ·········· shell  2m 10s"."""
        colors = {"agent": "38;2;175;135;255m", "shell": "38;2;110;160;220m"}
        out = []
        # Só os subagentes ganham linha; comandos e vigias ficam só na conta do "running in background".
        fan = [f for f in row.get("fan") or [] if f.get("kind") == "agent"]
        # E os "claude -p" que um script da sessão abriu (testadores, lotes): o Claude não os vê.
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
        """Responde a pergunta aberta (AskUserQuestion) pela tela da sessão. "1 2" escolhe uma
        opção por pergunta e envia; outro texto vai no "Type something." da pergunta da vez."""
        conn = _attached.get(row["id"])
        if not conn or conn.dead.is_set() or not conn.dialog():
            self.notice, self.notice_until = f"{row['name']}: the question is not open now", time.time() + 5
            return
        # A opção pode vir com caixinha na frente ("4. [ ] Type something"), na de marcar várias.
        option = lambda pat: next((m.group(1) for l in conn.dialog()
                                   for m in [re.match(r"\s*(?:❯\s*)?(\d+)\.\s*(?:\[.\]\s*)?" + pat, l)] if m), None)
        multi = any(re.match(r"\s*(?:❯\s*)?\d+\.\s*\[.\]", l) for l in conn.dialog())

        def go(want):
            """Leva o ❯ (a linha marcada) até a linha que want aceita, com ↑ / ↓."""
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
            for p in picks:   # na de marcar várias, cada número liga/desliga a caixinha
                conn.press(p)
                time.sleep(0.8)
            if multi and go(is_submit):
                conn.press("\r")
                time.sleep(0.8)
        elif multi:
            # Marcar várias: o texto entra na linha "Type something" (com o ❯ nela) e o Submit
            # da pergunta fica uma linha abaixo.
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
        # Respondeu todas: fica a aba "Submit". Manda, como o enter faria.
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
            # A conexão escondida tinha morrido: abre outra e aperta de novo, se a pergunta segue lá.
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
        """Quem espera você: as que pararam numa pergunta (a que espera há mais tempo
        primeiro) e as que acabaram de terminar e você ainda não olhou."""
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
        # Tudo floco, como no claude agents; a cor diz o estado. Laranja piscando: terminou e você não viu.
        if row["busy"]:
            return SPINNER[self.tick % len(SPINNER)]
        if row["group"] == "needs":
            return YELLOW + "✻" + RESET   # sem negrito: no terminal o negrito pinta o meio de branco
        if row["id"] in self.alerts and self.on("check_group"):
            # Terminou e você ainda não viu: o ✻ laranja acende e apaga a cada ~0,6 s (apagado é
            # um espaço; cinza ficava quase branco e laranja escuro mal se via piscar).
            return (ORANGE + "✻" + RESET) if (self.tick // 5) % 2 == 0 else " "   # sem negrito: pinta o meio de branco
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
        # Você começou a responder: já abre a sessão por baixo, e o Enter manda na hora.
        row = self.reply_to if self.mode == "reply" else None
        if isinstance(row, dict) and self.prompt.startswith("/"):
            session_commands(row)   # a lista completa de comandos chega enquanto você digita
        if isinstance(row, dict) and row.get("alive") and row.get("id") not in _attached \
                and getattr(self, "warming", None) != row["id"]:
            self.warming, self.warming_t = row["id"], time.time()
            self.background(lambda r: _safe(attach_open, r), row)

    def search_loop(self):
        # Procura nas conversas o que você digitou no ctrl+f, sem travar a tela.
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
                time.sleep(0.3)   # espera você parar de digitar
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
        """Os lugares da conversa desta sessão onde está o que você procura."""
        if not row or not self.find or self.hits_for != self.find:
            return []
        return self.find_hits.get(row["id"], [])

    def go_hit(self, row):
        """Vai até o achado escolhido: limpa a busca e rola a conversa até o prompt dele."""
        n, word = self.index_sel, self.find
        self.find, self.mode, self.find_hits, self.hits_for = "", "task", {}, ""
        self.follow(row["id"])
        self.index_sel = n
        self.index_go(row)
        self.notice, self.notice_until = f"\"{word}\" · prompt {n}", time.time() + 5

    def hits_list(self, hits, inner, area):
        """No lugar da conversa: cada lugar onde a palavra aparece; enter ou clique vão até lá."""
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
            head = f"prompt {n} · {'you' if who == 'você' else 'Claude'}"
            mark = i == sel
            where[len(lines)] = n
            lines.append((ESC + "48;2;45;55;72m" + BOLD + "▸ " if mark else "  ") + DIM + head + RESET)
            where[len(lines)] = n
            lines.append("  " + text)
        return lines[:area] + [""] * max(0, area - len(lines)), where

    def sync_reply_target(self, current=None):
        """Lista + conversa: escrevendo, a seta (ou o clique) que escolhe outra sessão leva a
        mensagem junto. A caixa, a conversa ao lado e o envio ficam sempre na mesma sessão."""
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
        # A última coluna fica livre: no Windows Terminal ela some atrás da borda
        # da janela e cortava a borda direita dos cards.
        cols = max(80, cols - 2)
        with self.lock:
            rows = list(self.rows)
            current = self.current()
            items = self.items()
        self.sync_reply_target(current)
        counts = {k: sum(1 for r in rows if r["group"] == k) for k in ("needs", "working", "done")}
        usage = plan_usage() if self.on("plan_usage") else ""
        info = [
            # Versão e, ao lado, o consumo do plano. O resto (modelo das sessões novas,
            # contagem por estado) já aparece na lista e nos grupos.
            f"{BOLD}Claude Code{RESET} {DIM}v{self.version}{RESET}" + (f"   {usage}" if usage else ""),
            "",
            "",
        ]
        # Sem botão "esperando você" no topo (você pediu para tirar); o f2 continua pulando.
        self.waiting_button = (0, 0, 0)
        # O alerta some quando você fica 3 s parado na sessão (passar por ela com a seta não conta).
        here = current[1]["id"] if current and current[0] == "row" else None
        if here != getattr(self, "here_id", None):
            self.here_id, self.here_since = here, time.time()
        if here in self.alerts and self.on("alert_clear") and time.time() - self.here_since > 3:
            self.alerts.pop(here, None)
            self.save_config()
            self.follow(here)   # ela volta para o grupo dela; a seleção vai junto
        lines = [f" {ORANGE}{LOGO[i]}{RESET}  {info[i]}" for i in range(3)] + [""]
        # Botões no canto de cima à direita: trocar a visão e Atualizar. O clique cai
        # em self.view_button / self.button.
        # Símbolos de 1 coluna: o ☰ ocupa 2 no terminal e empurrava a tela uma linha.
        view_label = ""   # sem botão de trocar a visão: só existe Lista + conversa
        used = 1 + text_width(LOGO[0]) + 2 + visible_width(info[0])
        help_w = text_width(HELP_LABEL) + 1
        feat_w = text_width(FEAT_LABEL) + 1
        start = max(used + 2 + help_w + feat_w, cols - text_width(RELOAD_LABEL) - 1 - text_width(view_label))
        self.help_button = (start - help_w + 1, start - 1)
        self.feat_button = (start - help_w - feat_w + 1, start - help_w - 1)
        self.view_button = (start + 1, start + text_width(view_label))
        reload_at = start + text_width(view_label) + 1
        self.button = (reload_at + 1, reload_at + text_width(RELOAD_LABEL))
        self.view_button = (0, -1)   # nada para clicar
        lit = ESC + "48;2;215;119;87m" + ESC + "38;2;20;20;20m"   # botão aceso (painel aberto)
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
                self.pane_hits = (None, [])   # sem conversa ao lado, nada nela é clicável
                self.pane_geo = None
                if self.view == "kanban" and self.peek:
                    # Tela dividida: Kanban à esquerda, a conversa do card escolhido à direita.
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
            # Lista + conversa: a lista principal à esquerda e a conversa ao vivo da
            # sessão escolhida à direita.
            bottom = self.bottom(cols)
            room = total - len(bottom) - len(lines)
            # A lista só tem nome, PRs e tempo: fica estreita e a conversa ganha o resto.
            left_w = 60
            body, sel_line = self.list_body(items, current, left_w, compact=True)
            start = max(0, min(sel_line - room // 2, len(body) - room))
            body = body[start:start + room]
            # Para o clique: a linha de tela (de 1) da 1ª linha da lista, o deslocamento e a largura.
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
            # Rolou: as 4 primeiras linhas ficam; a lista segue a partir de start + 4.
            if hdr <= 4:
                self.list_click = (5, max(4, start + 4) - hdr, cols, self.list_click[3])
        lines += [""] * (room - len(lines))
        self.paint(lines, bottom)

    def list_body(self, items, current, cols, compact=False):
        """Linhas da lista (títulos de grupo e sessões) e a posição da escolhida.
        compact tira modelo e effort, para caber ao lado da conversa."""
        lines, where = [], []   # where: o item de cada linha (None nas linhas em branco), para o clique
        name_w, model_w, effort_w, badge_w, age_w = (22, 0, 0, 8, 7) if compact else (22, 10, 7, 8, 7)
        summary_w = max(10, cols - 3 - name_w - 1 - model_w - 1 - effort_w - 1 - badge_w - 1 - age_w - 1)
        if compact:
            # Ao lado da conversa sai o resumo, os PRs e o tempo; no lugar entra a linha de status.
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
                # Linha escolhida: faixa cinza na largura toda, como o título do grupo.
                # Cada RESET do meio religa o fundo para a faixa não ter buracos.
                sel_line = len(lines)
                visible = visible_width(line)
                line = BAR + line.replace(RESET, RESET + BAR) + " " * max(0, cols - visible) + RESET
            lines.append(line)
            where.append(index)
        self.list_where = where
        return lines, sel_line

    def thumbs(self):
        """Imagens coladas (alt+v) que ainda estão no texto: [(número, arquivo)]."""
        images = getattr(self, "images", {})
        return [(n, images[n]) for n in re.findall(r"\[Imag(?:em|e) #(\d+)\]", self.prompt) if n in images
                and os.path.exists(images[n])]

    def tick_tooltip(self, frame):
        """Caixinha com o prompt do ┤ sob o mouse, à esquerda da barra da conversa."""
        hover = getattr(self, "tick_hover", None)
        row, ticks = getattr(self, "tick_hits", (None, []))
        if not hover or not row or row["id"] != hover[0]:
            return frame
        _, n, x, y = hover
        prompts = all_prompts(row.get("transcript"))
        # Prompts próximos caem no mesmo ┤ da barra (conversa longa, barra curta): a caixa
        # mostra todos os daquele ponto, um embaixo do outro, e não só o primeiro.
        here = sorted({k for _, _, yy, k in ticks if yy == y and 1 <= k <= len(prompts)}) or (
            [n] if 1 <= n <= len(prompts) else [])
        if not here:
            return frame
        width = min(70, max(20, x - 6))
        each = 4 if len(here) == 1 else 2   # várias: 2 linhas de cada
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
        left = max(0, x - 1 - width - 1)   # coluna (de 0) onde a caixa começa
        top = max(0, min(y - 1 - len(box) // 2, len(frame) - len(box)))
        frame = list(frame)
        color = ESC + "48;2;30;34;44m" + ESC + "38;2;220;225;235m"
        for k, l in enumerate(box):
            i = top + k
            if i < len(frame):
                frame[i] = slice_ansi(frame[i], 0, left) + RESET + color + l + RESET + slice_ansi(frame[i], left + width, float("inf"))
        return frame

    def paint(self, lines, bottom):
        """Escreve só as linhas que mudaram desde o último desenho. Assim as miniaturas
        (Sixel) das imagens coladas ficam na tela: reescrever a linha apagaria a imagem."""
        frame = self.tick_tooltip(lines + bottom)
        self.queue_top = len(lines) + getattr(self, "queue_offset", 0)   # linha (de 0) da 1ª da fila
        # As chaves do painel de features, na linha da tela (de 1) onde ficaram.
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
                    # Apaga a linha inteira antes: escrever por cima de emoji (2 colunas) deixava
                    # buraco no Windows Terminal (o 🧠 e o ⚡ sumiam numa linha da lista).
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
            # Metade de emoji que escapou vira "?" em vez de derrubar o programa.
            sys.stdout.write("".join(out).encode("utf-8", "replace").decode("utf-8"))
            sys.stdout.flush()
        self.last_frame, self.last_size = frame, size

    def bottom(self, cols):
        rows = self._bottom(cols)
        thumbs = self.thumbs()
        self.queue_offset = THUMB_ROWS if thumbs else 0   # a fila começa depois das miniaturas
        if not thumbs:
            return rows
        # Linhas livres para as miniaturas e, embaixo de cada uma, o número dela.
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
        # O que aparece na caixa e, para cada letra da tela, a posição dela no texto: o cursor
        # e o clique andam no texto de verdade. A quebra (ctrl+j, enter colado) pula a linha.
        text, back = raw, list(range(len(raw) + 1))
        pos = self.caret_pos() if self.mode != "find" else len(raw)
        cpos = back.index(pos) if pos in back else len(text)
        self.prompt_spans = []
        # Seta do prompt branca, como no original.
        if text or label:
            # Texto longo quebra em linhas que começam onde o texto começa (depois do "→ nome: "),
            # não no canto da tela.
            indent = 2 + text_width(label)
            # Cada linha do texto quebra sozinha; o "\n" fica no fim da linha dele (é onde o
            # cursor aparece quando está nele) e na tela vira espaço.
            spans, s = [], 0
            for part in text.split("\n"):
                e = s + len(part)
                pieces = [(s + a, s + b) for a, b in wrap_spans(part, max(10, cols - indent - 1))]
                if e < len(text):
                    pieces[-1] = (pieces[-1][0], e + 1)
                spans += pieces
                s = e + 1
            # Até 8 linhas, sempre com a do cursor à vista.
            at = next((k for k, (a, b) in enumerate(spans) if a <= cpos < b), len(spans) - 1)
            first = max(0, min(at - 7, len(spans) - 8))
            prompt, top = [], 0
            if first:
                # Texto colado maior que a caixa: a 1ª linha diz para quem vai e quanto ficou
                # acima (sem ela parecia que o começo do texto tinha sumido). Ela ocupa uma
                # das 8 linhas, então mostra 7 do texto, com a do cursor à vista.
                first = max(1, min(at - 6, len(spans) - 7))
                prompt.append(f"❯ {DIM}{label}↑ {first} more line{'s' if first > 1 else ''} above "
                              f"({len(text)} characters, all of it is sent){RESET}")
                top = 1
            spans = spans[first:first + 8 - top]
            for k, (a, b) in enumerate(spans):
                seg = text[a:b].replace("\n", " ")
                if a <= cpos < b:   # o cursor é a letra em vídeo inverso
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
            # Painel de features: duas colunas de chaves; o clique liga/desliga (self.feat_hits).
            title = f"  {BOLD}Features{RESET} {DIM}· click to turn on/off · saved for next time · esc or the button closes{RESET}"
            out = [rule, *prompt, rule, title]
            ncol = 3 if cols >= 170 else 2
            col_w = (cols - 4) // ncol
            self.feat_rel = []   # (linha dentro do rodapé, coluna inicial, final, chave)
            for area in dict.fromkeys(f[3] for f in FEATURES):   # uma área por vez, com o título dela
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
            self.prompt_rel = 1   # a 1ª linha da caixa, contando do começo do rodapé
            return [rule, *prompt, rule, title] + hints
        # O modo da sessão (auto mode, plan mode…) fica à esquerda da dica, como no Claude.
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
        """A fila logo acima da caixa, como no Codex: uma linha por mensagem, com os botões
        ↳ steer (manda agora, no meio do trabalho), ✎ (edita) e ✕ (cancela)."""
        qedit = getattr(self, "qedit", None)
        if qedit:
            row = next((r for r in self.rows if r["id"] == qedit["id"]), row)
        items = self.waitlist.get(row["id"]) if row else None
        self.queue_marks = []   # (linha dentro da fila, início, fim, ação, id, posição)
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
            x = text_width(left) + len(pad) + 1   # coluna (de 1) onde começa o "↳ steer"
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

    # ------------------------------------------------ ações

    def run_claude(self, args, cwd=None):
        ran = subprocess.run(["claude", *args], capture_output=True, text=True, encoding="utf-8", cwd=cwd)
        if ran.returncode == 0:
            lines = ran.stdout.strip().splitlines()
            return True, (lines[0] if lines else "")
        lines = (ran.stderr or ran.stdout).strip().splitlines()
        return False, (lines[0] if lines else f"exit {ran.returncode}")

    def new_session_dir(self):
        # O Claude não cria sessão em segundo plano na pasta do usuário ("Workspace
        # not trusted"). Aberto de lá, a sessão nova vai para a pasta da sessão
        # escolhida, que o Claude já confia.
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
                # Erro numa tarefa de fundo vai para o erro.log, não por cima do desenho da tela.
                import traceback
                try:
                    with open(os.path.join(DATA_DIR, "erro.log"), "a", encoding="utf-8") as f:
                        f.write(time.strftime("%d/%m %H:%M:%S ") + traceback.format_exc() + "\n")
                except OSError:
                    pass
                self.notice, self.notice_until = f"error in {getattr(fn, '__name__', 'task')} · see erro.log", time.time() + 6
        threading.Thread(target=run, daemon=True).start()

    def new_session(self, task):
        where = self.new_session_dir()
        self.notice = f"starting a new session in {home_path(where)}…"
        ok, msg = self.run_claude(["--bg", task], cwd=where)
        self.notice = f"new session · {msg}" if ok else f"error: {msg}"
        self.refresh()

    def reply(self, row, text):
        # Sessão viva: entra nela por baixo (claude attach) e digita, como o claude
        # agents faz. Trabalhando ou não, a mensagem chega na hora e o modelo
        # decide quando ler. Viva, ela nunca é parada para reabrir: se não deu para
        # digitar, fica o aviso. Parada (sem processo): retoma com a mensagem.
        # A mensagem já aparece no fim da conversa, com "sending…" e depois "✓".
        if text.strip().lower() in ("/exit", "/quit"):
            # Na conexão escondida, /exit só a tira da sessão (volta para a lista do claude
            # agents) e a sessão segue rodando. Aqui ele faz o que se espera: encerra a sessão.
            with _attached_lock:
                old = _attached.pop(row["id"], None)
            if old:
                old.close()
            # Fica na conversa, como as mensagens: "/exit" e o que aconteceu.
            done = {"text": text.strip(), "state": "enviando", "t": time.time()}
            with self.lock:
                self.outbox.setdefault(row["id"], []).append(done)
            if not row["alive"]:
                done["state"] = "fechada"
                self.notice, self.notice_until = f"{row['name']} is already closed", time.time() + 5
                return
            self.notice, self.notice_until = f"closing {row['name']}…", time.time() + 10
            # Primeiro o /exit entra na sessão de verdade (fica gravado na conversa dela). Lá ele
            # só tira a conexão escondida de dentro; quem fecha a sessão é o stop logo depois.
            _safe(attach_send, row, text.strip())
            with _attached_lock:
                old = _attached.pop(row["id"], None)
            if old:
                old.close()
            ok, msg = self.run_claude(["stop", row["id"]])
            done["state"], done["why"] = ("fechada", "") if ok else ("erro", msg)
            self.notice, self.notice_until = (f"{row['name']} closed · your next message continues the conversation"
                                              if ok else f"could not close {row['name']}: {msg}"), time.time() + 8
            self.refresh()
            return
        sent_msg = {"text": text, "state": "enviando", "t": time.time()}
        with self.lock:
            box = self.outbox.setdefault(row["id"], [])
            box[:] = [m for m in box if m["state"] != "fechada"] + [sent_msg]   # reabriu: o "/exit" sai
        try:
            if row["alive"]:
                sent, msg = attach_send(row, text)
                if sent is False and "question or permission" not in msg:
                    # A sessão pode ter reiniciado bem nessa hora (o Claude se atualiza e
                    # reabre as sessões): conexão nova, espera ela voltar e manda de novo.
                    sent_msg["state"] = "enviando"
                    with _attached_lock:
                        old = _attached.pop(row["id"], None)
                    if old:
                        old.close()
                    for _ in range(30):
                        if any(s.get("id") == row["id"] and s.get("pid") for s in agents_json()):
                            break
                        time.sleep(1)
                    sent, msg = attach_send(row, text)
                sent_msg["state"] = "ok" if sent else "incerto" if sent is None else "erro"
                sent_msg["why"] = msg
                if sent and text.startswith("/"):
                    # O comando pode abrir um menu na sessão (/model, /config…): ele aparece aqui.
                    self.panel = {"id": row["id"], "t": time.time(), "seen": False}
                if not sent:
                    self.notice, self.notice_until = f"{row['name']}: {msg}", time.time() + 15
                self.refresh()
                return
            self.notice, self.notice_until = f"sending to {row['name']}… (it reopens, takes ~15 s)", time.time() + 120
            # Fechada: o claude attach reabre a sessão com o mesmo id e a mesma conversa. O
            # --resume ficou só de reserva: em sessão antiga ele abria a conversa errada ou uma cópia.
            sent, msg = attach_send(row, text)
            if sent is not False:
                sent_msg["state"], sent_msg["why"] = ("ok" if sent else "incerto"), msg
                self.notice, self.notice_until = f"sent to {row['name']} ✓", time.time() + 15
                self.refresh()
                return
            with _attached_lock:
                old = _attached.pop(row["id"], None)
            if old:
                old.close()
            ok, msg = self.resume_with(row, text)
            sent_msg["state"], sent_msg["why"] = ("ok" if ok else "erro"), msg
            self.notice, self.notice_until = (f"sent to {row['name']} ✓" if ok else f"error: {msg}"), time.time() + 15
            self.refresh()
        except Exception as exc:   # o aviso e a mensagem nunca ficam presos em "sending…"
            sent_msg["state"], sent_msg["why"] = "erro", str(exc)
            self.notice, self.notice_until = f"error sending to {row['name']}: {exc}", time.time() + 15

    def resume_with(self, row, text):
        """Sessão parada: retoma com a mensagem, sob o mesmo id. Se o --resume criar uma
        cópia com outro id (a sessão ainda contava como rodando), apaga a cópia e tenta de novo."""
        ok, msg = False, "could not reopen the session (each try opened a copy with another id)"
        for _ in range(3):
            if any(s.get("id") == row["id"] and s.get("pid") for s in agents_json()):
                self.run_claude(["stop", row["id"]])
                for _ in range(30):
                    if not any(s.get("id") == row["id"] and s.get("pid") for s in agents_json()):
                        break
                    time.sleep(1)
                time.sleep(2)
            # A conversa de verdade é o arquivo que a sessão grava (em sessão antiga, o sessionId
            # do state.json aponta para outra, vazia).
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
        # Ida: a nossa tela fica desenhada também na tela de baixo do terminal. Ao
        # sair da sessão, o Claude leva um tempo fechando e mostra essa tela de
        # baixo: assim aparece a lista, e não uma tela preta.
        with _attached_lock:
            if row["id"] in _attached:
                _attached.pop(row["id"]).close()
        self.console(False)
        self.notice, self.notice_until = f"coming back from {row['name']}…", 0
        self.last_frame = None
        sys.stdout.write(ESC + "?1049l")
        self.draw()
        # Cursor no topo: o texto que o Claude escreve ao sair da sessão cai em
        # cima da lista (e some no redesenho), em vez de no pé da tela, onde
        # empurrava tudo para cima.
        sys.stdout.write(ESC + "H" + ESC + "?25h")
        sys.stdout.flush()
        # Na pasta da sessão: o ← dentro dela abre o claude agents original, e na
        # pasta do usuário ele parava perguntando se a pasta é confiável.
        where = row.get("cwd") if row.get("cwd") and os.path.isdir(row["cwd"]) else None
        attached = subprocess.Popen(["claude", "attach", row["id"]], cwd=where)
        # O ← dentro da sessão abre o claude agents original como filho do attach.
        # Quando esse filho aparece, fechamos os dois e voltamos para esta tela.
        while attached.poll() is None:
            if any(name.lower() == "claude.exe" for name in descendants(attached.pid).values()):
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(attached.pid)], capture_output=True)
                attached.wait()
                sys.stdout.write(ESC + "?1049l")
                break
            time.sleep(0.15)
        # Volta: desliga os modos que a sessão deixou no terminal e desenha na hora
        # com os dados que já temos; a lista atualiza por trás. Fica na tela de
        # baixo, que já mostra a lista: trocar de tela piscava preto.
        sys.stdout.write(TERMINAL_RESET + ESC + "?25l")
        sys.stdout.flush()
        self.last_frame = None
        self.console(True)
        self.notice = ""
        self.mark_seen(row["id"])
        self.draw()
        self.background(self.refresh)

    # ------------------------------------------------ teclado

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
        # Tab passa por Lista → Kanban → Lista + conversa → Lista.
        self.view = {"list": "kanban", "kanban": "split"}.get(self.view, "list")
        self.drag = None
        self.save_config()
        if item and item[0] == "row":
            self.follow(item[1]["id"])

    def mouse(self, kind, x, y):
        """Mouse: ('down'|'up'|'dbl', coluna, linha), contando de 1.
        No Kanban: clicar escolhe o card, arrastar e soltar em outra coluna move,
        clique duplo abre a sessão."""
        if kind == "move":
            # Mouse num ┤ da barra da conversa: mostra o prompt dele numa caixinha ao lado.
            row, ticks = getattr(self, "tick_hits", (None, []))
            hit = next(((n, x0, yy) for x0, x1, yy, n in ticks if yy == y and x0 <= x <= x1), None) if row else None
            hover = (row["id"], *hit) if hit else None
            if hover != getattr(self, "tick_hover", None):
                self.tick_hover = hover
                return True
        if kind == "down" and self.prompt and self.mode != "find":
            # Clique no texto que você está escrevendo: o cursor vai para a letra clicada.
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
            # Botões da fila, acima da caixa: ↳ steer, ✎ e ✕.
            for i, x0, x1, action, sid, idx in getattr(self, "queue_marks", []):
                if y == getattr(self, "queue_top", -9) + i + 1 and x0 <= x <= x1:
                    self.queue_action(action, sid, idx)
                    return True
        if kind in ("down", "dbl") and self.view in ("list", "split") and getattr(self, "list_click", None):
            # Clique numa sessão (ou num título de grupo) da lista escolhe; clique duplo abre.
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
            # Conversa ao lado: ☰ índice abre / fecha a lista; clicar num prompt dela,
            # ou num ┤ da barra de rolagem, sobe a conversa até ele.
            row, hits = getattr(self, "pane_hits", (None, []))
            hit = next((h for h in hits if h[0] <= x <= h[1] and h[2] == y), None)
            try:   # cliques.log: onde foi o clique e os botões daquela linha (para achar clique perdido)
                if not self.on("click_log"):
                    raise OSError   # desligado no painel de features
                log_path = os.path.join(DATA_DIR, "cliques.log")
                if os.path.exists(log_path) and os.path.getsize(log_path) > 512 * 1024:
                    os.replace(log_path, log_path + ".1")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} x={x} y={y} hit={hit[3] if hit else None} "
                            f"linha={[(h[0], h[1], h[3]) for h in hits if h[2] == y][:8]}\n")
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
                    # O "Copiar" do Windows leva ~3 s: roda por trás, e o aviso diz quando ficou pronto.
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
                    # Explorer com o arquivo selecionado (o /select, vai junto no mesmo argumento).
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
            # Painéis: clicar escolhe (na lista da esquerda ou no painel), clique duplo abre.
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
            # Passar o mouse por cima do card mostra o pop-up; sair dele esconde.
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
            # Mouse andando: só atualiza o pop-up; não fecha atalhos, avisos nem o ctrl+x pendente.
            self.mouse(*ch)
            return True
        if ch == "\x1b" and getattr(self, "arts_open", None):   # esc fecha a lista de artifacts
            self.arts_open = None
            return True
        if isinstance(ch, tuple) and ch[0] == "down" and ch[2] == 1 and self.feat_button[0] <= ch[1] <= self.feat_button[1]:
            self.feat_open, self.help = not self.feat_open, False   # botão ⊞ Features abre/fecha o painel
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
                        self.features.pop(hit[3])   # ligada é o padrão: não precisa guardar
                    self.save_config()
                    name = next(f[1] for f in FEATURES if f[0] == hit[3])
                    self.notice, self.notice_until = f"{name}: {'on' if self.on(hit[3]) else 'off'}", time.time() + 3
                    return True
        item = self.current()
        if isinstance(ch, tuple) and (ch[0] in ("move", "up") or (
                ch[0] == "down" and ch[2] == 1 and self.help_button[0] <= ch[1] <= self.help_button[1])):
            # Mouse andando ou soltando o botão não é tecla: não fecha os atalhos, não
            # cancela o que está em andamento. O clique em Atalhos abre e fecha.
            if ch[0] == "down":
                self.help = not self.help
                return True
            # Mouse andando sem mudar nada volta "skip": não redesenha e, mais importante,
            # não é o False que fecha o programa.
            return self.mouse(*ch) or "skip"
        if ch not in ("\x1b", "\x03") and not (isinstance(ch, tuple) and ch[0] in ("move", "up")):
            self.quitting = 0   # qualquer outra tecla (ou clique) desiste de fechar
            self.stopping = (None, 0)   # e de parar a sessão
            if not isinstance(ch, tuple):
                self.sel = None   # tecla apaga a marca da seleção (o texto já foi copiado)
        if getattr(self, "moving", None):
            # Movendo um grupo inteiro: ↑↓ (ou ←→ no Kanban) mudam o lugar; enter, esc ou ctrl+g terminam.
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
        if time.time() >= self.notice_until:  # aviso de envio fica na tela
            self.notice = ""

        if isinstance(ch, tuple):
            done = self.mouse(*ch)
            return "skip" if done is None else done
        if ch == "F2":
            self.jump_waiting()
            return True
        if ch == "S-TAB":
            # Escrevendo para uma sessão: troca o modo dela; senão, o da sessão escolhida.
            target = self.reply_to if self.mode == "reply" and isinstance(self.reply_to, dict) \
                else item[1] if item and item[0] == "row" else None
            if target and target.get("alive"):
                self.background(self.switch_mode, target)
            return True
        # alt+↑: abre a fila para editar, mudar a ordem ou cancelar (como no Codex).
        if ch == "\x00" and self.queue and self.queue[0] == ALT_ARROWS["H"] and not self.prompt.strip() \
                and not getattr(self, "qedit", None):
            row = self.reply_to if self.mode == "reply" and isinstance(self.reply_to, dict) \
                else item[1] if item and item[0] == "row" else None
            if row and self.waitlist.get(row["id"]):
                self.getch()
                self.mode, self.reply_to, self.prompt = "task", None, ""
                self.qedit = {"id": row["id"], "sel": len(self.waitlist[row["id"]]) - 1}
                return True
        if getattr(self, "qedit", None) and not isinstance(ch, tuple):   # fila aberta: as teclas são dela
            return self.queue_key(ch)
        # Escrevendo: ←/→, home/end e del andam e apagam dentro do texto (o cursor).
        if not self.prompt:
            self.caret = None
        typing = self.mode in ("reply", "group", "rename") or (self.mode == "task" and self.prompt)
        if typing and ch == "\xe0" and self.queue and self.queue[0] in ("K", "M", "G", "O", "S", "s", "t"):
            self.caret_key(self.getch())
            return True
        panel = getattr(self, "panel", None)
        if panel and item and item[0] == "row" and item[1]["id"] == panel["id"] and self.mode == "task" and not self.prompt:
            # Menu aberto por um comando / na sessão: setas, Enter, Esc e letras vão para ele.
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
                and time.time() >= getattr(self, "paste_until", 0)   # texto colado que começa com número
                and item and item[0] == "row"):
            # Pergunta ou permissão aberta na sessão: o número escolhe a opção lá.
            conn = _attached.get(item[1]["id"])
            if conn and not conn.dead.is_set() and conn.dialog():
                self.background(self.press_option, conn, ch, item[1])
                return True
        if ch == "F5":   # atualiza o programa (pega o código novo); o botão do topo faz o mesmo
            self.reload = True
            return False
        if ch == "\x12" and self.mode == "task" and not self.prompt and item:
            # Ctrl+R, como no claude agents: renomeia a sessão escolhida ou, no título
            # de um grupo seu, o grupo.
            if item[0] == "head" and item[1].startswith("custom:"):
                self.mode, self.reply_to, self.prompt = "rename", ("group", item[1][7:]), item[1][7:]
            elif item[0] == "row":
                self.mode, self.reply_to, self.prompt = "rename", item[1], item[1]["name"]
            return True
        if ch in ("\x13", "\t") and not (self.mode in ("reply", "find", "group", "rename") or self.prompt):
            return True   # tab trocava a visão; agora só existe Lista + conversa

        found = (self.hits_of(item[1]) if self.view in ("kanban", "split") and item and item[0] == "row"
                 and self.mode in ("find", "task") and not self.prompt else [])
        if found:
            # Achados da busca no lugar da conversa: ctrl+↑↓ escolhe, enter vai até lá.
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
            # ctrl+u: índice dos seus prompts; ↑↓ / PgUp PgDn escolhem, enter vai, esc fecha.
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

        # Fila, como no Codex: tab guarda a mensagem para quando a sessão terminar a vez dela.
        if ch == "\t" and self.mode == "reply" and isinstance(self.reply_to, dict) and self.prompt.strip():
            self.sync_reply_target()
            row = self.reply_to
            if not self.save_queued_edit():
                self.enqueue(row, self.with_images(self.prompt.strip()))
            self.prompt, self.mode, self.reply_to = "", "task", None
            return True
        if ch == "\xe0" and self.queue and self.queue[0] == "S":   # Delete fora da fila: nada
            self.getch()
            return True

        if ch == "\x00" and self.queue and self.queue[0] == "V":
            self.getch()
            self.paste_image(item)
            return True

        if (ch in ("\x00", "\xe0") and self.queue and self.queue[0] in ("I", "Q", "\x8d", "\x91")
                and (self.view == "split" or (self.view == "kanban" and self.popup_target()))):
            # Rolam a conversa ao lado, mesmo enquanto você digita: ctrl+↑/↓ de 3 em 3
            # linhas, PgUp/PgDn de meia tela.
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
            if ch == "\x01" and task:   # Ctrl+A: mais um agente na mesma tarefa
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
            self.hover = None   # setas mandam: o pop-up segue o card escolhido, não o mouse
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
            elif code == "s":   # Shift+← leva o card para a coluna da esquerda
                self.move_card(row, self.kcol - 1)
            elif code == "t":   # Shift+→ leva o card para a coluna da direita
                self.move_card(row, self.kcol + 1)
            elif code in ("U", "D"):   # Shift+↑↓ muda a coluna do grupo de lugar
                self.move_group(item, -1 if code == "U" else 1)
            return True

        if ch in ("\x00", "\xe0"):
            code = self.getch()
            moves = {"H": -1, "P": 1, "I": -10, "Q": 10}
            if self.recall(code):
                return True
            if self.mode == "group" and code in ("H", "P"):
                # Na caixa do grupo, ↑ ↓ trocam entre os grupos que já existem.
                order, _ = self.custom()
                if order:
                    pos = order.index(self.prompt) if self.prompt in order else (-1 if code == "P" else 0)
                    self.prompt = order[(pos + (1 if code == "P" else -1)) % len(order)]
                return True
            if code in moves:
                self.cursor += moves[code]
                self.pscroll = 0
            elif code == "M":
                # Seta para a direita faz o mesmo que o Enter (abrir a sessão ou
                # recolher o grupo), menos quando você está digitando.
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
                if editing_q:   # editando uma da fila: esc mantém o texto antigo e volta para a fila
                    self.qediting = None
                    items = self.waitlist.get(editing_q[0]) or []
                    if editing_q[1] in items:
                        self.qedit = {"id": editing_q[0], "sel": items.index(editing_q[1])}
            elif self.prompt or self.compose_new:
                self.prompt, self.compose_new = "", False
            else:
                # Em cima de uma sessão trabalhando, o esc para o trabalho dela (como o esc
                # dentro do Claude Code); sem isso, pergunta se fecha o programa.
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
                    self.names[what["id"]] = name   # já aparece com o nome novo na lista
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
                self.save_queued_edit()   # editando uma da fila: o enter grava no lugar dela
                self.prompt, self.mode, self.reply_to = "", "task", None
            elif self.mode == "reply":
                self.sync_reply_target()   # a seta pode ter vindo junto, sem desenho no meio
                text, row = self.prompt.strip(), self.reply_to
                self.prompt, self.mode, self.reply_to = "", "task", None
                fresh = next((r for r in self.rows if r["id"] == row["id"]), row) if row else None
                if text and fresh and fresh.get("questions") and self.dialog_of(fresh):
                    # Pergunta aberta na tela da sessão: o texto vira a resposta dela.
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
        elif ch == "\x17":   # Ctrl+W (e Ctrl+Backspace em alguns terminais): apaga a última palavra
            if self.mode == "find":
                self.find = self.find.rstrip()[: len(self.find.rstrip()) - len(self.find.rstrip().split(" ")[-1])]
            else:
                p = self.caret_pos()   # apaga a palavra antes do cursor
                kept = self.prompt[:p].rstrip()
                kept = kept[: len(kept) - len(kept.split(" ")[-1])]
                self.prompt = kept + self.prompt[p:]
                if getattr(self, "caret", None) is not None:
                    self.caret = len(kept)
        elif ch in ("\b", "\x7f"):
            if self.mode == "find":
                self.find = self.find[:-1]
            else:
                p = self.caret_pos()   # apaga a letra antes do cursor
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
        elif ch in ("\x10", "\x14") and item and item[0] == "row":   # ctrl+p ou ctrl+t
            # Fixar tira a sessão do grupo dela: a seleção vai junto, senão o
            # próximo Ctrl+P cairia em outra sessão.
            self.toggle_pin(item[1])
            self.follow(item[1]["id"])
        elif ch == "\x07" and self.mode == "task" and not self.prompt and self.group_of(item) and (
                self.view == "kanban" or (item and item[0] == "head")):
            # Ctrl+G no título de um grupo seu (no Kanban, na coluna dele): ↑↓ levam o grupo inteiro.
            self.start_moving(self.group_of(item))
        elif ch == "\x07" and self.mode == "group" and self.reply_to and self.custom()[1].get(self.reply_to["id"]):
            # Ctrl+G de novo na caixa do grupo: passa a mover o grupo da sessão.
            name = self.custom()[1][self.reply_to["id"]]
            self.prompt, self.mode, self.reply_to = "", "task", None
            self.start_moving(name)
        elif ch == "\x07" and item and item[0] == "row" and self.mode == "task" and not self.prompt:
            # Ctrl+G: põe a sessão num grupo seu (Stand-by, por exemplo).
            self.mode, self.reply_to = "group", item[1]
            self.prompt = self.custom()[1].get(item[1]["id"], "")
        elif ch == "\x18" and item:
            if self.confirm is not None and self.confirm[:2] == item[:2]:
                self.confirm = None
                if item[0] == "head" and item[1].startswith("custom:"):
                    # Desfazer um grupo seu só tira as sessões dele; não apaga nada.
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
        """Onde está o cursor no texto (None = no fim, o normal)."""
        c = getattr(self, "caret", None)
        return len(self.prompt) if c is None else max(0, min(c, len(self.prompt)))

    def insert_text(self, s):
        p = self.caret_pos()
        self.prompt = self.prompt[:p] + s + self.prompt[p:]
        if getattr(self, "caret", None) is not None:
            self.caret = p + len(s)
            if self.caret >= len(self.prompt):
                self.caret = None   # chegou ao fim: volta ao normal

    def caret_key(self, code):
        """←/→ andam uma letra, home/end vão ao começo/fim, del apaga a letra do cursor."""
        p, n = self.caret_pos(), len(self.prompt)
        if code == "K":
            p -= 1
        elif code == "M":
            p += 1
        elif code == "s":   # shift+← também anda
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

    # ------------------------------------------------ entrada (teclado e mouse)

    def getch(self):
        """Próxima tecla já traduzida (a fila guarda o segundo código das setas)."""
        while not self.queue:
            self.queue += self.read_events() or []
            if not self.queue:
                time.sleep(0.01)
        return self.queue.pop(0)

    def pending(self):
        return bool(self.queue) or self.events_waiting()

    def read_events(self):
        """Teclas já traduzidas: setas como "\\xe0"+"H"/"P", Alt+seta como
        "\\x00"+"\\x98"/"\\xa0", clique como (coluna, linha) contando de 1.

        Conforme o terminal, a seta chega como tecla do Windows ou como texto que
        começa com Esc ("\\x1b[A", "\\x1bOA" ou o formato do Windows Terminal
        "\\x1b[38;72;0;1;0;1_"). Aqui tudo vira a mesma tecla; Esc sozinho só vale
        quando nada vem logo depois dele."""
        raw = self.raw_events()
        self.log_keys(raw)
        # Texto colado chega como teclas, muitas de uma vez, e a quebra de linha dele vem
        # como Enter: mandava a mensagem no meio da colagem. Numa rajada (e logo depois
        # dela), o Enter vira quebra de linha, como o ctrl+j.
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
            if raw and raw[0] in ("v", "V"):   # Alt+V chega como Esc + v
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
                        break  # sequência cortada: descarta
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
        # Emoji (e outro caractere fora do básico) chega em duas metades, uma por tecla:
        # junta as duas numa letra só. Metade solta era o que derrubava o desenho da tela.
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
        # Colagem: muitas letras de uma vez. Conta e troca depois de decifrar as teclas (no
        # Windows Terminal cada tecla chega como um código "\x1b[…_", e o tab e o enter só
        # aparecem aqui). Na rajada, o enter vira quebra de linha (como o ctrl+j) e o tab vira
        # espaço: cada tab de uma tabela colada virava uma mensagem na fila da sessão.
        now = time.time()
        typed = [c for i, c in enumerate(merged) if isinstance(c, str) and len(c) == 1 and c >= " "
                 and c != "\xe0" and not (i and merged[i - 1] in ("\xe0", "\x00"))]   # setas seguradas não contam
        # Cada leitura traz poucas teclas (uma tecla são vários eventos): conta pelo tempo,
        # somando as leituras. 4 letras em 60 ms ninguém digita; é colagem.
        recent = [t for t in getattr(self, "typed_at", []) if now - t < 0.06] + [now] * len(typed)
        self.typed_at = recent[-50:]
        waiting = bool(merged) and _safe(self.events_waiting)   # ainda tem tecla na fila do console
        if len(recent) >= 4 or (waiting and len(typed) >= 1):
            self.paste_until = now + 0.25
        if now < getattr(self, "paste_until", 0) and self.on("paste_guard"):
            merged = [p for c in merged for p in (["\n"] if c == "\r" else [" "] * 4 if c == "\t" else [c])]
        return merged

    def log_keys(self, raw):
        """Guarda as últimas 300 teclas cruas em teclas.log (só com AGENTES_KEYLOG=1)."""
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
                elif vk == 0x0D and (state & 0x10):   # Shift+Enter: nova linha, como o ctrl+j
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
                    # Mouse andando: 32 arrastando com o botão apertado (seleção), 35 sem botão
                    # (pop-up). Sem isso o arrastar não chegava e a seleção ficava vazia.
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
        """Eventos crus do console do Windows (tecla ou mouse)."""
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
                    elif k.wVirtualKeyCode == 0x74:   # F5: atualiza o programa
                        out.append("F5")
                    elif k.wVirtualKeyCode == 0x71:   # F2: próxima sessão que espera você
                        out.append("F2")
                    elif k.wVirtualKeyCode == 0x09 and shift:   # shift+tab: troca o modo da sessão
                        out.append("S-TAB")
                    elif k.wVirtualKeyCode == 0x0D and shift:   # Shift+Enter: nova linha, como o ctrl+j
                        out.append("\n")
                    elif k.uChar and k.uChar != "\x00":  # Shift, Ctrl sozinhos vêm com 0
                        out.append(k.uChar)
            elif rec.EventType == MOUSE_EVENT:
                m = rec.Event.MouseEvent
                x, y = m.dwMousePosition.X + 1, m.dwMousePosition.Y + 1
                if m.dwEventFlags == 0 and m.dwButtonState & 1:
                    out.append(("down", x, y))
                elif m.dwEventFlags == 0 and not m.dwButtonState:
                    out.append(("up", x, y))
                elif m.dwEventFlags & 0x1:  # mouse andando (sem botão ou arrastando)
                    out.append(("move", x, y))
                elif m.dwEventFlags & 0x2:  # clique duplo
                    out.append(("dbl", x, y))
                elif m.dwEventFlags & 0x4:  # roda do mouse
                    up = ctypes.c_int16(m.dwButtonState >> 16).value > 0
                    out += ["\xe0", "W" if up else "w"]
        return out

    def read_input(self):
        return self.getch()

    def keep_mouse(self):
        """Religa o mouse a cada desenho. Outro programa no mesmo console (o claude que
        manda sua resposta, a volta do ctrl+r) pode desligá-lo; aí a rodinha chega como
        seta e troca de sessão em vez de rolar a conversa."""
        if not getattr(self, "mouse_on", False):   # desligado de propósito (dentro de uma sessão)
            return
        mode = ctypes.c_uint32()
        ctypes.windll.kernel32.GetConsoleMode(self.handle, ctypes.byref(mode))
        want = (mode.value | 0x0080 | 0x0010 | 0x0008) & ~(0x0040 | 0x0001 | 0x0200)
        if mode.value != want:
            ctypes.windll.kernel32.SetConsoleMode(self.handle, want)
        sys.stdout.write(ESC + "?1003h" + ESC + "?1006h")

    def console(self, on):
        """Liga/desliga o mouse do console (desliga antes do attach, religa na volta)."""
        self.handle = ctypes.windll.kernel32.GetStdHandle(-10)
        if on:
            mode = ctypes.c_uint32()
            ctypes.windll.kernel32.GetConsoleMode(self.handle, ctypes.byref(mode))
            self.input_mode = mode.value
            # Mouse e janela ligados; edição rápida, Ctrl+C do sistema e entrada VT
            # desligados, para as setas e o clique chegarem como eventos do Windows.
            new = (mode.value | 0x0080 | 0x0010 | 0x0008) & ~(0x0040 | 0x0001 | 0x0200)
            ctypes.windll.kernel32.SetConsoleMode(self.handle, new)
            # O Windows Terminal só repassa o clique se o programa pedir o mouse (sem isso,
            # só a rodinha chega, virando seta): 1003 = clique e movimento, 1006 = coordenadas longas.
            sys.stdout.write(ESC + "?1003h" + ESC + "?1006h")
            sys.stdout.flush()
            self.mouse_on = True
        elif self.input_mode is not None:
            self.mouse_on = False
            sys.stdout.write(ESC + "?1003l" + ESC + "?1006l")
            sys.stdout.flush()
            ctypes.windll.kernel32.SetConsoleMode(self.handle, self.input_mode)

    def run(self):
        os.system("")  # liga as cores ANSI no console do Windows
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
                    # Várias teclas/movimentos juntos (texto colado): desenha uma vez só no fim.
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
    # Se o programa quebrar, o erro fica em erro.log e ele abre de novo sozinho
    # (menos quando quebra logo ao abrir, para não ficar abrindo e quebrando).
    started = time.time()
    try:
        sys.exit(App().run())
    except Exception:
        import traceback
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "erro.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now():%d/%m %H:%M:%S}\n{traceback.format_exc()}\n")
        sys.stdout.write(ESC + "?25h" + ESC + "?1049l")
        print(f"The program crashed. The error was saved to {log}")
        sys.exit(RELOAD_EXIT if time.time() - started > 10 else 1)
