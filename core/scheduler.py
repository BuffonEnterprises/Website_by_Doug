"""Rotinas agendadas (cron) — "todo dia às 8h me faz o briefing".

Persiste em SQLite, checa a cada 30s via croniter e publica routine.fired no
bus; o loop principal transforma isso num turno autônomo do agente. As tools
deixam o Claude criar/listar/remover rotinas por voz.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from datetime import datetime

from skills.registry import ToolSpec


class Scheduler:
    def __init__(self, db_path: str, bus, tz: str = "America/Sao_Paulo"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.bus = bus
        self.tz = tz
        self._lock = threading.Lock()
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS routines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, cron TEXT, prompt TEXT,
                enabled INTEGER DEFAULT 1,
                last_run REAL DEFAULT 0)"""
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    def add(self, name: str, cron: str, prompt: str) -> str:
        try:
            from croniter import croniter
            if not croniter.is_valid(cron):
                return f"[erro] expressão cron inválida: {cron}"
        except ImportError:
            return "[erro] agendador indisponível — pip install croniter"
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO routines (name, cron, prompt, last_run) VALUES (?, ?, ?, ?)",
                (name, cron, prompt, time.time()),
            )
            self.conn.commit()
        return f"[ok] rotina '{name}' criada (#{cur.lastrowid}, cron `{cron}`)"

    def remove(self, rid: int) -> str:
        with self._lock:
            cur = self.conn.execute("DELETE FROM routines WHERE id = ?", (rid,))
            self.conn.commit()
        return "[ok] rotina removida" if cur.rowcount else "[erro] id não encontrado"

    def list_all(self) -> str:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM routines ORDER BY id").fetchall()
        if not rows:
            return "(nenhuma rotina)"
        return "\n".join(
            f"#{r['id']} {r['name']} [{r['cron']}] "
            f"{'on' if r['enabled'] else 'off'}: {r['prompt'][:60]}"
            for r in rows
        )

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        try:
            from croniter import croniter
        except ImportError:
            return
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.tz)
        except Exception:  # noqa: BLE001
            tz = None
        while True:
            now = time.time()
            with self._lock:
                rows = self.conn.execute(
                    "SELECT * FROM routines WHERE enabled = 1").fetchall()
            for r in rows:
                base = datetime.fromtimestamp(r["last_run"], tz)
                try:
                    nxt = croniter(r["cron"], base).get_next(float)
                except Exception:  # noqa: BLE001
                    continue
                if nxt <= now:
                    with self._lock:
                        self.conn.execute(
                            "UPDATE routines SET last_run = ? WHERE id = ?", (now, r["id"]))
                        self.conn.commit()
                    self.bus.publish("routine.fired",
                                     {"name": r["name"], "prompt": r["prompt"]})
            await asyncio.sleep(30)

    # ------------------------------------------------------------------ #
    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec("routine_add",
                     "Cria uma rotina agendada. cron = 5 campos (min hora dia mês dia-semana). "
                     "Ex.: '0 8 * * *' = todo dia 8h. prompt = a instrução a executar.",
                     {"type": "object", "properties": {
                         "name": {"type": "string"},
                         "cron": {"type": "string"},
                         "prompt": {"type": "string"}},
                      "required": ["name", "cron", "prompt"]},
                     "low", lambda a, c: self.add(a["name"], a["cron"], a["prompt"])),
            ToolSpec("routine_list", "Lista as rotinas agendadas.",
                     {"type": "object", "properties": {}, "required": []},
                     "read", lambda a, c: self.list_all()),
            ToolSpec("routine_remove", "Remove uma rotina pelo id.",
                     {"type": "object", "properties": {"id": {"type": "integer"}},
                      "required": ["id"]},
                     "medium", lambda a, c: self.remove(int(a.get("id", 0))),
                     lambda a: f"Vou remover a rotina #{a.get('id')}."),
        ]
