"""Motor de política de segurança — defesa em profundidade.

Princípios (após o red team):
  • A denylist e a classificação HIGH valem para TODA capacidade destrutiva, não
    só `run_shell`: AppleScript com `do shell script`/`send`/`delete` é HIGH e passa
    pela denylist do shell embutido; caminhos catastróficos são barrados no próprio
    handler (mac.py), independentemente do modo.
  • Nada de auto-allow por engano: redirecionamento, smuggling (`$()`, crase,
    newline) e programas fora da allowlist nunca caem em READ.
  • Confirmação exige afirmativa forte; filler não confirma; kill-phrase sempre vence.

Níveis:  read · low · medium · high     Modos: always_confirm · safe_confirm · autonomous · yolo
"""
from __future__ import annotations

import os
import re
import shlex

READ, LOW, MEDIUM, HIGH = "read", "low", "medium", "high"

# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

_SAFE_SHELL = {
    "ls", "pwd", "cat", "head", "tail", "echo", "date", "cal", "whoami",
    "hostname", "df", "du", "ps", "uptime", "which", "type", "env", "printenv",
    "grep", "egrep", "rg", "wc", "file", "stat", "sw_vers", "uname",
    "system_profiler", "sysctl", "man", "tree", "basename", "dirname",
    "realpath", "sort", "uniq", "diff", "column", "jq", "pbpaste", "id",
}
_GIT_READ = {"status", "log", "diff", "show", "branch", "remote", "shortlog",
             "blame", "describe", "rev-parse", "ls-files"}

# smuggling / redirecionamento: presença ⇒ nunca é READ (mínimo MEDIUM)
_SMUGGLE = re.compile(r"[\n`<>]|\$\(|\$\{|\|\|")

_DANGER_MEDIUM = re.compile(
    r"(\bmv\b|\bcp\b|\bmkdir\b|\btouch\b|\bbrew\b|\bpip3?\b|\bnpm\b|\bnpx\b|"
    r"\bcurl\b|\bwget\b|\btee\b|\bln\b|\btruncate\b|sed\s+-i|\bxargs\b|"
    r"\bpython3?\b|\bnode\b|\bbash\b|\bsh\b|\bzsh\b|\bosascript\b|\bmake\b|"
    r"\bdocker\b|defaults\s+write|\bfind\b)",
    re.IGNORECASE,
)
_DANGER_HIGH = re.compile(
    r"(\bsudo\b|\brm\b|\brmdir\b|\bshutdown\b|\breboot\b|\bhalt\b|\bmkfs|\bdd\b|"
    r"\bdiskutil\b|\bkillall\b|\bkill\b|\bpkill\b|\bcsrutil\b|\bsecurity\b|"
    r"\blaunchctl\b|\bnvram\b|\bchmod\b|\bchown\b|\bcrontab\b|"
    r"-delete\b|-exec(dir)?\b|-fprint|-fls\b|"
    r"\|\s*(sh|bash|zsh|python3?|node|osascript)\b|"           # pipe-to-shell
    r"git\s+push\s+.*--force)",
    re.IGNORECASE,
)
# NUNCA roda, em nenhum modo (barreira absoluta)
# barreira absoluta: raízes NUAS (/, ~, //, /*) e dirs de sistema; NÃO pega subpastas
# legítimas (rm -rf ~/proj/build passa e só cai em HIGH->confirma). Aspas contam como fim.
_DENYLIST = re.compile(
    r"\brm\b\s+(-{1,2}[a-zA-Z]+\s+)*("
    r"(/|~|\$HOME|//|/\*)(\s|\*|$|[\"'])"
    r"|(/Users|/System|/Library|/etc|/bin|/usr|/sbin|/var|/Applications)(\s|/|\*|$|[\"']))"
    r"|:\s*\(\s*\)\s*\{|\bmkfs|of=/dev/|>\s*/dev/(sd|disk|r?disk)"
    r"|diskutil\s+(erase|reformat|partitiondisk|zerodisk)|\bnvram\b|\bcsrutil\s+disable",
    re.IGNORECASE,
)

_AS_HIGH = ("do shell script", "delete", "send", "system events", "keystroke",
            "key code", "empty trash", "restart", "shut down")
_AS_WRITE = ("make ", "set ", "create", "add ", "remove", "quit", "close",
             "save", "play", "pause", "activate", "move", "duplicate", "open ")


def shell_risk(command: str) -> str:
    cmd = (command or "").strip()
    if not cmd:
        return MEDIUM
    if _DANGER_HIGH.search(cmd):
        return HIGH
    if _SMUGGLE.search(cmd):          # redirect, $(), crase, newline, ${}
        return MEDIUM
    if _DANGER_MEDIUM.search(cmd):
        return MEDIUM
    for segment in re.split(r"[|;&]+", cmd):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return MEDIUM
        if not tokens:
            continue
        prog = os.path.basename(tokens[0])
        if prog == "git":
            if (tokens[1] if len(tokens) > 1 else "") not in _GIT_READ:
                return MEDIUM
            continue
        if prog not in _SAFE_SHELL:
            return MEDIUM
    return READ


def applescript_risk(script: str) -> str:
    s = (script or "").lower()
    if any(h in s for h in _AS_HIGH):
        return HIGH
    return MEDIUM if any(v in s for v in _AS_WRITE) else READ


def denylisted(spec_risk: str, args: dict) -> bool:
    """Barreira absoluta, checada antes de qualquer modo."""
    if spec_risk == "dynamic:shell":
        return bool(_DENYLIST.search(args.get("command", "") or ""))
    if spec_risk == "dynamic:applescript":
        s = args.get("script", "") or ""
        return bool(_DENYLIST.search(s))   # pega `do shell script "rm -rf /"`
    return False


# ---------------------------------------------------------------------------
# Decisão
# ---------------------------------------------------------------------------

class Policy:
    def __init__(self, cfg: dict):
        safety = cfg.get("safety", {})
        self.mode = safety.get("mode", "safe_confirm")
        self.kill_phrases = [k.lower() for k in safety.get("kill_phrases", ["para tudo"])]
        self.kill_flag = False

    def resolve_risk(self, spec_risk: str, args: dict) -> str:
        if spec_risk == "dynamic:shell":
            return shell_risk(args.get("command", ""))
        if spec_risk == "dynamic:applescript":
            return applescript_risk(args.get("script", ""))
        return spec_risk

    def decide(self, spec_risk: str, args: dict) -> str:
        """Retorna 'allow' | 'confirm' | 'deny'."""
        if denylisted(spec_risk, args):
            return "deny"
        risk = self.resolve_risk(spec_risk, args)
        if self.mode == "yolo":
            return "allow"
        if self.mode == "autonomous":
            return "confirm" if risk == HIGH else "allow"
        if self.mode == "always_confirm":
            return "allow" if risk == READ else "confirm"
        # safe_confirm (padrão): read/low livres; medium/high confirmam
        return "allow" if risk in (READ, LOW) else "confirm"

    # ------------------------------------------------------------------ #
    def is_kill(self, text: str) -> bool:
        t = (text or "").lower()
        return any(k in t for k in self.kill_phrases)

    # afirmativas fortes (sem filler ambíguo como "vai", "faz", "aham")
    _YES = {"sim", "pode", "claro", "isso", "confirmo", "confirma", "confirmar",
            "autorizo", "autorizado", "aprovado", "ok", "okay", "beleza",
            "positivo", "exato", "exatamente", "perfeito", "certo", "correto",
            "manda", "yes"}
    _NO = {"não", "nao", "nunca", "cancela", "cancelar", "para", "pare", "chega",
           "negativo", "no", "deixa", "esquece", "espera", "melhor", "aborta"}

    def parse_confirm(self, answer: str) -> bool:
        raw = (answer or "").lower()
        if self.is_kill(raw):
            self.kill_flag = True
            return False
        words = set(re.findall(r"\w+", raw))
        if words & self._NO:      # um "não" claro sempre vence
            return False
        return bool(words & self._YES)   # senão, exige afirmativa forte explícita
