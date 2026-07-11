"""Tarefas em segundo plano — trabalho longo sem travar a conversa.

`spawn_task` roda um mini-agente autônomo (só ferramentas de leitura/baixo
risco, para segurança) numa asyncio.Task. O Leo continua falando; quando
termina, o resultado é publicado no bus e falado ("terminei aquela pesquisa").
"""
from __future__ import annotations

import asyncio
import time

from skills.registry import ToolSpec


class TaskManager:
    def __init__(self, cfg: dict, bus):
        self.bus = bus
        self.max_concurrent = int(cfg.get("tasks", {}).get("max_concurrent", 3))
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._tasks: dict[int, dict] = {}
        self._live: set = set()   # referência forte às tasks (evita GC prematuro)
        self._seq = 0
        self.run_background = None  # injetado pelo Assistant: async (prompt, tid) -> str

    def _track(self, t):
        self._live.add(t)
        t.add_done_callback(self._live.discard)

    def spawn(self, prompt: str, label: str = "") -> int:
        self._seq += 1
        tid = self._seq
        self._tasks[tid] = {"label": label or prompt[:40], "status": "running",
                            "started": time.time(), "result": None}
        self._track(asyncio.create_task(self._runner(tid, prompt)))
        self._purge()
        return tid

    def _purge(self, keep: int = 100) -> None:
        if len(self._tasks) <= keep:
            return
        done = [k for k, v in self._tasks.items() if v["status"] in ("done", "error")]
        for k in sorted(done)[: len(self._tasks) - keep]:
            self._tasks.pop(k, None)

    async def _runner(self, tid: int, prompt: str) -> None:
        async with self._sem:
            self.bus.publish("task.start", {"id": tid, "label": self._tasks[tid]["label"]})
            try:
                result = await self.run_background(prompt, tid)
                self._tasks[tid].update(status="done", result=result)
            except Exception as e:  # noqa: BLE001
                self._tasks[tid].update(status="error", result=str(e))
            self.bus.publish("task.done", {
                "id": tid, "label": self._tasks[tid]["label"],
                "status": self._tasks[tid]["status"],
                "result": (self._tasks[tid]["result"] or "")[:1500],
            })

    def spawn_call(self, coro_factory, label: str = "") -> int:
        """Roda uma corrotina arbitrária em segundo plano (usado por sub-agentes/planner)."""
        self._seq += 1
        tid = self._seq
        self._tasks[tid] = {"label": label or "tarefa", "status": "running",
                            "started": time.time(), "result": None}
        self._track(asyncio.create_task(self._run_call(tid, coro_factory)))
        self._purge()
        return tid

    async def _run_call(self, tid: int, coro_factory) -> None:
        async with self._sem:
            self.bus.publish("task.start", {"id": tid, "label": self._tasks[tid]["label"]})
            try:
                result = await coro_factory()
                self._tasks[tid].update(status="done", result=result)
            except Exception as e:  # noqa: BLE001
                self._tasks[tid].update(status="error", result=str(e))
            self.bus.publish("task.done", {
                "id": tid, "label": self._tasks[tid]["label"],
                "status": self._tasks[tid]["status"],
                "result": (self._tasks[tid]["result"] or "")[:1500],
            })

    def status(self) -> str:
        if not self._tasks:
            return "(nenhuma tarefa)"
        return "\n".join(
            f"#{tid} [{t['status']}] {t['label']}" for tid, t in self._tasks.items()
        )

    def tool_specs(self) -> list[ToolSpec]:
        async def spawn(args, ctx):   # async -> roda no event loop (create_task funciona)
            if self.run_background is None:
                return "[erro] tarefas de fundo indisponíveis"
            tid = self.spawn(args["prompt"], args.get("label", ""))
            return (f"[ok] tarefa #{tid} rodando em segundo plano; "
                    f"eu te aviso quando terminar.")

        return [
            ToolSpec("spawn_task",
                     "Dispara uma tarefa demorada em segundo plano (pesquisa longa, varredura "
                     "de arquivos, análise). Não trava a conversa; o resultado é avisado ao terminar. "
                     "A tarefa só usa ferramentas de leitura.",
                     {"type": "object", "properties": {
                         "prompt": {"type": "string"},
                         "label": {"type": "string"}},
                      "required": ["prompt"]},
                     "low", spawn),
            ToolSpec("task_status", "Mostra o estado das tarefas em segundo plano.",
                     {"type": "object", "properties": {}, "required": []},
                     "read", lambda a, c: self.status()),
        ]
