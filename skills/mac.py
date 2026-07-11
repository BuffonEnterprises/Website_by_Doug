"""Skills de controle do macOS: terminal, arquivos, AppleScript e apps nativos."""
from __future__ import annotations

import os
import shutil
import subprocess

from skills.registry import ToolSpec


# ---------------------------------------------------------------------------
# Primitivas
# ---------------------------------------------------------------------------

def _sh(cmd: list[str] | str, shell: bool = False, timeout: int = 60,
        cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=shell, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def run_applescript(script: str, timeout: int = 30) -> str:
    try:
        proc = _sh(["osascript", "-e", script], timeout=timeout)
    except subprocess.TimeoutExpired:
        return "[timeout] o AppleScript demorou demais."
    except FileNotFoundError:
        return "[erro] osascript não encontrado (isto só roda no macOS)."
    out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"[erro applescript] {err or out}"
    return out or "[ok]"


def _esc(s: str) -> str:
    # escapa para caber dentro de um literal "..." de AppleScript, inclusive quebras de linha
    return ((s or "").replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


def _preview(text: str, n: int = 200) -> str:
    """Prévia legível para a confirmação — mantém a CAUDA visível (onde mora o perigo)."""
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:130] + " … " + t[-60:]


def _as_describe(a: dict) -> str:
    s = a.get("script", "")
    if "do shell script" in s.lower():
        return f"Vou rodar um comando de shell via automação: {_preview(s)}."
    return f"Vou executar uma automação no Mac: {_preview(s)}."


# Barreiras absolutas de caminho (valem em QUALQUER modo, inclusive yolo)
_PROTECTED_ROOTS = {os.path.realpath(os.path.expanduser(p)) for p in (
    "/", "~", "/Users", "/System", "/Library", "/etc", "/var", "/bin", "/usr",
    "/sbin", "/Applications", "~/Library", "~/Documents", "~/Desktop", "~/.ssh",
)}
_REFUSE_WRITE = ("/library/launchagents/", "/library/launchdaemons/",
                 "/.ssh/authorized_keys", "/.ssh/id_", "/etc/", "/private/etc/",
                 "/cron")


def _is_protected_root(path: str) -> bool:
    try:
        return os.path.realpath(os.path.expanduser(path or "")) in _PROTECTED_ROOTS
    except Exception:  # noqa: BLE001
        return True


def _is_refused_write(path: str) -> bool:
    try:
        p = os.path.realpath(os.path.expanduser(path or "")).lower()
    except Exception:  # noqa: BLE001
        return True
    return any(frag in p for frag in _REFUSE_WRITE)


# ---------------------------------------------------------------------------
# Handlers (args, ctx) -> str
# ---------------------------------------------------------------------------

def h_run_shell(args: dict, ctx) -> str:
    command = args.get("command", "")
    workdir = os.path.expanduser(ctx.cfg["agent"].get("workdir", "~"))
    try:
        proc = _sh(command, shell=True, timeout=int(args.get("timeout", 90)), cwd=workdir)
    except subprocess.TimeoutExpired:
        return "[timeout] comando interrompido."
    parts = []
    if (proc.stdout or "").strip():
        parts.append(proc.stdout.strip())
    if (proc.stderr or "").strip():
        parts.append(f"[stderr] {proc.stderr.strip()}")
    parts.append(f"[exit {proc.returncode}]")
    result = "\n".join(parts)
    return result[:8000] + "\n[...truncado...]" if len(result) > 8000 else result


def h_read_file(args: dict, ctx) -> str:
    path = os.path.expanduser(args.get("path", ""))
    if not os.path.isfile(path):
        return f"[erro] arquivo não encontrado: {path}"
    try:
        with open(path, "r", errors="replace") as f:
            data = f.read(12001)
    except Exception as e:  # noqa: BLE001
        return f"[erro] {e}"
    return data[:12000] + ("\n[...truncado...]" if len(data) > 12000 else "")


def h_list_dir(args: dict, ctx) -> str:
    path = os.path.expanduser(args.get("path", "~") or "~")
    if not os.path.isdir(path):
        return f"[erro] pasta não encontrada: {path}"
    entries = []
    for name in sorted(os.listdir(path))[:300]:
        full = os.path.join(path, name)
        entries.append(name + ("/" if os.path.isdir(full) else ""))
    return "\n".join(entries) or "(pasta vazia)"


def h_write_file(args: dict, ctx) -> str:
    if _is_refused_write(args.get("path", "")):
        return "[bloqueado] escrever nesse caminho sensível (LaunchAgents/.ssh/etc) é proibido."
    path = os.path.expanduser(args.get("path", ""))
    content = args.get("content", "")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(path, "w") as f:
            f.write(content)
    except Exception as e:  # noqa: BLE001
        return f"[erro] {e}"
    return f"[ok] {len(content)} caracteres escritos em {path}"


def h_move_path(args: dict, ctx) -> str:
    if _is_protected_root(args.get("src", "")) or _is_protected_root(args.get("dst", "")):
        return "[bloqueado] mover de/para uma pasta de sistema/raiz é proibido."
    src = os.path.expanduser(args.get("src", ""))
    dst = os.path.expanduser(args.get("dst", ""))
    if not os.path.exists(src):
        return f"[erro] origem não existe: {src}"
    try:
        shutil.move(src, dst)
    except Exception as e:  # noqa: BLE001
        return f"[erro] {e}"
    return f"[ok] movido: {src} -> {dst}"


def h_delete_path(args: dict, ctx) -> str:
    if _is_protected_root(args.get("path", "")):
        return "[bloqueado] apagar uma pasta de sistema/raiz é proibido."
    path = os.path.expanduser(args.get("path", ""))
    if not os.path.exists(path):
        return f"[erro] não existe: {path}"
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except Exception as e:  # noqa: BLE001
        return f"[erro] {e}"
    return f"[ok] apagado: {path}"


def h_applescript(args: dict, ctx) -> str:
    return run_applescript(args.get("script", ""))


def h_send_imessage(args: dict, ctx) -> str:
    to, text = _esc(args.get("recipient", "")), _esc(args.get("text", ""))
    return run_applescript(f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{to}" of targetService
        send "{text}" to targetBuddy
    end tell''')


def h_create_note(args: dict, ctx) -> str:
    title, body = _esc(args.get("title", "Sem título")), _esc(args.get("body", ""))
    return run_applescript(
        f'tell application "Notes" to make new note at folder "Notes" '
        f'with properties {{name:"{title}", body:"{body}"}}\nreturn "ok"'
    )


def h_add_reminder(args: dict, ctx) -> str:
    title = _esc(args.get("title", ""))
    due = args.get("due", "")  # "YYYY-MM-DD HH:MM" opcional
    if due:
        try:
            from datetime import datetime
            dt = datetime.strptime(due, "%Y-%m-%d %H:%M")
            asdate = dt.strftime("date \"%d/%m/%Y %H:%M\"")  # nem sempre parseia; fallback abaixo
            script = (
                f'tell application "Reminders" to make new reminder with properties '
                f'{{name:"{title}", due date:(current date) + '
                f'{int((dt - datetime.now()).total_seconds())}}}'
            )
            _ = asdate
        except ValueError:
            return "[erro] formato de data inválido; use YYYY-MM-DD HH:MM"
    else:
        script = (f'tell application "Reminders" to make new reminder '
                  f'with properties {{name:"{title}"}}')
    return run_applescript(script + '\nreturn "ok"')


def h_list_reminders(args: dict, ctx) -> str:
    return run_applescript(
        'tell application "Reminders" to get name of reminders whose completed is false',
        timeout=30,
    )


def h_calendar_today(args: dict, ctx) -> str:
    return run_applescript('''
    set output to ""
    set today to current date
    set startOfDay to today - (time of today)
    set endOfDay to startOfDay + 1 * days
    tell application "Calendar"
        repeat with cal in calendars
            set evs to (every event of cal whose start date ≥ startOfDay and start date < endOfDay)
            repeat with ev in evs
                set output to output & (time string of (start date of ev)) & " — " & (summary of ev) & linefeed
            end repeat
        end repeat
    end tell
    return output''', timeout=45) or "(sem eventos hoje)"


def h_spotify(args: dict, ctx) -> str:
    action = (args.get("action", "") or "").lower()
    cmds = {
        "play": 'tell application "Spotify" to play',
        "pause": 'tell application "Spotify" to pause',
        "next": 'tell application "Spotify" to next track',
        "previous": 'tell application "Spotify" to previous track',
        "current": ('tell application "Spotify" to return (name of current track)'
                    ' & " — " & (artist of current track)'),
    }
    if action not in cmds:
        return "[erro] use: play, pause, next, previous, current"
    return run_applescript(cmds[action])


def h_notify(args: dict, ctx) -> str:
    text, title = _esc(args.get("text", "")), _esc(args.get("title", "Jarvis"))
    return run_applescript(f'display notification "{text}" with title "{title}"')


def h_clipboard_read(args: dict, ctx) -> str:
    try:
        proc = _sh(["pbpaste"], timeout=10)
        return (proc.stdout or "")[:8000] or "(clipboard vazio)"
    except Exception as e:  # noqa: BLE001  (FileNotFoundError, TimeoutExpired…)
        return f"[erro] pbpaste: {e}"


def h_clipboard_write(args: dict, ctx) -> str:
    try:
        subprocess.run(["pbcopy"], input=args.get("text", ""), text=True, timeout=10)
        return "[ok] copiado para o clipboard"
    except Exception as e:  # noqa: BLE001
        return f"[erro] pbcopy: {e}"


def h_open(args: dict, ctx) -> str:
    target = args.get("target", "")
    if not args.get("is_app"):
        low = target.lower().split("?")[0]
        if low.endswith((".command", ".app", ".dmg", ".terminal", ".workflow",
                         ".scpt", ".sh", ".pkg", ".mpkg", ".shortcut")):
            return "[bloqueado] abrir esse tipo de arquivo pode executar código; recusado."
    try:
        proc = _sh(["open"] + (["-a", target] if args.get("is_app") else [target]), timeout=15)
    except Exception as e:  # noqa: BLE001
        return f"[erro] open: {e}"
    return "[ok]" if proc.returncode == 0 else f"[erro] {proc.stderr.strip()}"


def h_set_volume(args: dict, ctx) -> str:
    try:
        level = max(0, min(100, int(args.get("level", 50))))
    except (TypeError, ValueError):
        return "[erro] level deve ser 0-100"
    return run_applescript(f"set volume output volume {level}")


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

def tool_specs() -> list[ToolSpec]:
    S = ToolSpec
    return [
        S("run_shell",
          "Roda um comando bash no terminal do Mac. É a ferramenta mais versátil: "
          "sistema, git, scripts, projetos. Leitura roda direto; escrita passa por confirmação.",
          {"type": "object", "properties": {
              "command": {"type": "string"},
              "timeout": {"type": "integer", "description": "segundos, padrão 90"}},
           "required": ["command"]},
          "dynamic:shell", h_run_shell,
          lambda a: f"Vou rodar um comando que altera o sistema: {_preview(a.get('command', ''))}."),
        S("read_file", "Lê um arquivo de texto.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
          "read", h_read_file),
        S("list_dir", "Lista arquivos e pastas de um diretório.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
          "read", h_list_dir),
        S("write_file", "Cria ou sobrescreve um arquivo.",
          {"type": "object", "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
           "required": ["path", "content"]},
          "medium", h_write_file,
          lambda a: f"Vou escrever no arquivo {a.get('path', '')}."),
        S("move_path", "Move ou renomeia arquivo/pasta.",
          {"type": "object", "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
           "required": ["src", "dst"]},
          "medium", h_move_path,
          lambda a: f"Vou mover {a.get('src', '')} para {a.get('dst', '')}."),
        S("delete_path", "Apaga arquivo ou pasta (recursivo).",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
          "high", h_delete_path,
          lambda a: f"Vou apagar {a.get('path', '')}. Isso não tem volta."),
        S("applescript",
          "Executa AppleScript para controlar qualquer app do Mac (Calendário, Mail, "
          "Finder, Safari...). Scripts de leitura rodam direto; os que mudam algo confirmam.",
          {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
          "dynamic:applescript", h_applescript, _as_describe),
        S("send_imessage", "Envia iMessage (telefone, e-mail ou nome do contato).",
          {"type": "object", "properties": {"recipient": {"type": "string"},
                                             "text": {"type": "string"}},
           "required": ["recipient", "text"]},
          "high", h_send_imessage,
          lambda a: f"Vou mandar pra {a.get('recipient', '')}: \"{a.get('text', '')[:80]}\"."),
        S("create_note", "Cria nota no app Notas.",
          {"type": "object", "properties": {"title": {"type": "string"},
                                             "body": {"type": "string"}},
           "required": ["title"]},
          "low", h_create_note),
        S("add_reminder", "Cria um lembrete no app Lembretes (due opcional: YYYY-MM-DD HH:MM).",
          {"type": "object", "properties": {"title": {"type": "string"},
                                             "due": {"type": "string"}},
           "required": ["title"]},
          "low", h_add_reminder),
        S("list_reminders", "Lista lembretes pendentes.",
          {"type": "object", "properties": {}, "required": []}, "read", h_list_reminders),
        S("calendar_today", "Lista os eventos de hoje no Calendário (pode demorar alguns segundos).",
          {"type": "object", "properties": {}, "required": []}, "read", h_calendar_today),
        S("spotify", "Controla o Spotify: play, pause, next, previous, current.",
          {"type": "object", "properties": {"action": {"type": "string",
              "enum": ["play", "pause", "next", "previous", "current"]}},
           "required": ["action"]},
          "low", h_spotify),
        S("notify", "Mostra notificação na tela do Mac.",
          {"type": "object", "properties": {"text": {"type": "string"},
                                             "title": {"type": "string"}},
           "required": ["text"]},
          "low", h_notify),
        S("clipboard_read", "Lê o conteúdo atual do clipboard.",
          {"type": "object", "properties": {}, "required": []}, "read", h_clipboard_read),
        S("clipboard_write", "Copia um texto para o clipboard.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
          "low", h_clipboard_write),
        S("open", "Abre um app (is_app=true) ou um arquivo/URL no app padrão.",
          {"type": "object", "properties": {"target": {"type": "string"},
                                             "is_app": {"type": "boolean"}},
           "required": ["target"]},
          "medium", h_open,
          lambda a: f"Vou abrir: {a.get('target', '')}."),
        S("set_volume", "Ajusta o volume do Mac (0 a 100).",
          {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]},
          "low", h_set_volume),
    ]
