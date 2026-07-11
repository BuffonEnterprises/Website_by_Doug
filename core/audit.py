"""Trilha de auditoria: toda ação, decisão de política e uso de tokens.

É o que permite perguntar "Jarvis, o que você fez hoje?" e ter resposta
honesta — e é pré-requisito de qualquer autonomia responsável.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta


class Audit:
    def __init__(self, db_path: str, bus=None):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.bus = bus
        self._lock = threading.Lock()   # DB é tocado pela thread do loop e por worker threads
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                source TEXT,           -- voice | dashboard | routine | task
                tool TEXT,
                args TEXT,
                decision TEXT,         -- allow | confirm_yes | refused | deny | error
                result TEXT,
                ms INTEGER
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                model TEXT,
                tokens_in INTEGER,
                tokens_out INTEGER
            )
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    def log_action(self, source: str, tool: str, args: dict,
                   decision: str, result: str, ms: int) -> None:
        try:
            args_s = json.dumps(args, ensure_ascii=False, default=str)[:800]
        except Exception:  # noqa: BLE001
            args_s = str(args)[:800]
        with self._lock:
            self.conn.execute(
                "INSERT INTO actions (ts, source, tool, args, decision, result, ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), source, tool, args_s, decision, str(result)[:500], ms),
            )
            self.conn.commit()
        if self.bus:
            self.bus.publish("audit", {
                "tool": tool, "decision": decision, "ms": ms,
                "args": args_s[:200],
            })

    def log_llm(self, model: str, tokens_in: int, tokens_out: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO llm_usage (ts, model, tokens_in, tokens_out) VALUES (?, ?, ?, ?)",
                (time.time(), model, int(tokens_in or 0), int(tokens_out or 0)),
            )
            self.conn.commit()

    # ------------------------------------------------------------------ #
    def summary(self, hours: int = 24) -> str:
        since = time.time() - hours * 3600
        with self._lock:
            rows = self.conn.execute(
                "SELECT tool, decision, COUNT(*) FROM actions WHERE ts > ? "
                "GROUP BY tool, decision ORDER BY COUNT(*) DESC LIMIT 30",
                (since,),
            ).fetchall()
            usage = self.conn.execute(
                "SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), COUNT(*) "
                "FROM llm_usage WHERE ts > ?",
                (since,),
            ).fetchone()
        start = (datetime.now() - timedelta(hours=hours)).strftime("%d/%m %H:%M")
        lines = [f"Atividade desde {start}:"]
        if not rows:
            lines.append("- nenhuma ação executada.")
        for tool, decision, n in rows:
            lines.append(f"- {tool} ({decision}): {n}x")
        lines.append(
            f"- LLM: {usage[2]} chamadas, {usage[0]} tokens de entrada, "
            f"{usage[1]} de saída."
        )
        return "\n".join(lines)

    def recent(self, limit: int = 15) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, source, tool, decision, ms FROM actions "
                "ORDER BY ts DESC LIMIT ?", (limit,),
            ).fetchall()
        return [
            {"quando": datetime.fromtimestamp(r[0]).strftime("%H:%M:%S"),
             "origem": r[1], "tool": r[2], "decisao": r[3], "ms": r[4]}
            for r in rows
        ]
