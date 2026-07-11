"""Carregamento de configuração com defaults profundos (deep-merge).

O config.yaml do usuário sobrepõe apenas o que ele declarar; todo o resto
vem de DEFAULTS — assim o Jarvis nunca quebra por chave ausente.
"""
from __future__ import annotations

import copy
import os

DEFAULTS: dict = {
    "llm": {
        "model": "claude-fable-5",        # Claude Fable 5 (1M contexto, 128K saída)
        "heavy_model": "claude-fable-5",
        "max_tokens": 2048,
        "temperature": 0.3,
    },
    "timezone": "America/Sao_Paulo",
    "voice": {
        "input": "auto",  # auto | wake | ptt | text
        "wake": {"model": "hey_jarvis", "threshold": 0.55},
        "stt": {"engine": "auto", "model": "small", "language": "pt"},
        "tts": {
            "engine": "say",  # say | elevenlabs | openai | none
            "voice": "Luciana",
            "rate": 190,
            "elevenlabs_voice_id": "",
            "elevenlabs_model": "eleven_multilingual_v2",
            "openai_voice": "alloy",
            "stream_sentences": True,
        },
    },
    "safety": {
        "mode": "safe_confirm",  # always_confirm | safe_confirm | autonomous | yolo
        "kill_phrases": ["para tudo", "pare tudo", "aborta tudo"],
    },
    "memory": {
        "db_path": "memory/jarvis.db",
        "auto_recall": True,
        "embeddings": {
            "enabled": True,
            "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        },
    },
    "agent": {
        "max_tool_iterations": 16,
        "workdir": "~",
        "history_turns": 20,
    },
    "tasks": {"max_concurrent": 3},
    "routing": {"enabled": False, "fast_model": "claude-fable-5"},
    "state": {"enabled": True, "db_path": "memory/state/sessions.db",
              "session_id": "leo-main"},
    "knowledge": {"db_path": "memory/state/knowledge.db"},
    "logging": {"level": "INFO", "dir": "logs"},
    "dashboard": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8737,
        "speak_replies": False,
    },
    "mcp_servers": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if v is None:
            continue  # "chave:" vazia no YAML -> mantém o default (não vira None)
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str) -> dict:
    user: dict = {}
    if os.path.isfile(path):
        import yaml

        with open(path) as f:
            user = yaml.safe_load(f) or {}
    return _deep_merge(DEFAULTS, user)
