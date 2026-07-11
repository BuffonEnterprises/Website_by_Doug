"""Persistência de sessão + serialização de mensagens.

Guarda o histórico da conversa e o "resumo rolante" em SQLite, para o Jarvis
não ser amnésico após reiniciar. Serializa os blocos de conteúdo da Anthropic
(pydantic) para dict JSON e de volta — a API aceita ambos na volta.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time


def _block_to_dict(b):
    if isinstance(b, dict):
        return b
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(b, attr, None)
        if callable(fn):
            try:
                return fn(mode="json") if attr == "model_dump" else fn()
            except TypeError:
                try:
                    return fn()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
    return {"type": "text", "text": str(getattr(b, "text", b))}


def serialize_messages(messages: list) -> list:
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
        elif isinstance(content, list):
            out.append({"role": m["role"], "content": [_block_to_dict(b) for b in content]})
        else:
            out.append({"role": m["role"], "content": str(content)})
    return out


class SessionStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                updated REAL, summary TEXT, data TEXT)"""
        )
        self.conn.commit()

    def save(self, session_id: str, messages: list, summary: str = "") -> None:
        try:
            data = json.dumps(serialize_messages(messages), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self.conn.execute(
                "INSERT INTO sessions (session_id, updated, summary, data) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "updated=excluded.updated, summary=excluded.summary, data=excluded.data",
                (session_id, time.time(), summary, data),
            )
            self.conn.commit()

    def load(self, session_id: str) -> tuple[list, str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT data, summary FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return [], ""
        try:
            return json.loads(row[0]), (row[1] or "")
        except Exception:  # noqa: BLE001
            return [], (row[1] or "")
