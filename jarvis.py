#!/usr/bin/env python3
"""Jarvis v2 — orquestrador.

Sobe todos os subsistemas (memória híbrida, auditoria, política, agendador,
tarefas de fundo, MCP, painel web) e roda o loop de voz assíncrono:

    wake word / Enter  ->  escuta  ->  cérebro age e fala em streaming  ->  repete

Uso:
    python3 jarvis.py            # voz (wake word se disponível, senão push-to-talk)
    python3 jarvis.py --text     # digitar em vez de falar
    python3 jarvis.py --mode ptt # força push-to-talk
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from core import obs
from core.audit import Audit
from core.brain import Brain
from core.bus import Bus
from core.config import load_config
from core.context import Context
from core.planner import Planner
from core.policy import Policy
from core.router import ModelRouter
from core.scheduler import Scheduler
from core.state import SessionStore
from core.tasks import TaskManager
from senses.ears import Ears
from senses.mouth import Mouth
from senses.ultravision import UltraVision
from server.dashboard import Dashboard
from skills.knowledge import KnowledgeBase
from skills.mcp_client import MCPManager
from skills.memory import Embedder, SecondBrain
from skills.registry import AUTONOMOUS_DENY_TOOLS, build_registry

HERE = os.path.dirname(os.path.abspath(__file__))
EXIT_WORDS = {"sair", "tchau", "encerrar", "desligar", "adeus", "quit", "exit"}


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(HERE, path)


class Assistant:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.running = True
        self.bus = Bus()

        # subsistemas
        self.audit = Audit(_abs("memory/state/audit.db"), self.bus)
        self.policy = Policy(cfg)
        self.memory = SecondBrain(_abs(cfg["memory"]["db_path"]), Embedder(cfg))
        self.scheduler = Scheduler(_abs("memory/state/routines.db"), self.bus,
                                   cfg.get("timezone", "America/Sao_Paulo"))
        self.tasks = TaskManager(cfg, self.bus)
        self.mcp = MCPManager(cfg)

        # v3: inteligência + estado
        self.router = ModelRouter(cfg)
        st = cfg.get("state", {})
        self.state = (SessionStore(_abs(st.get("db_path", "memory/state/sessions.db")))
                      if st.get("enabled", True) else None)
        self.knowledge = KnowledgeBase(
            _abs(cfg.get("knowledge", {}).get("db_path", "memory/state/knowledge.db")),
            self.memory.emb)

        # contexto compartilhado + registry de ferramentas
        self.ctx = Context(cfg=cfg, bus=self.bus, memory=self.memory, audit=self.audit,
                           policy=self.policy, tasks=self.tasks, scheduler=self.scheduler,
                           mcp=self.mcp, router=self.router, state=self.state,
                           knowledge=self.knowledge,
                           session_id=st.get("session_id", "leo-main"))
        self.planner = Planner(self.ctx)
        self.ctx.planner = self.planner
        self.vision = UltraVision(self.ctx)
        self.ctx.vision = self.vision
        self.ctx.confirm = self._confirm

        # voz + cérebro
        self.ears = Ears(cfg)
        self.mouth = Mouth(cfg, self.bus)
        self.vision.speak = self.mouth.say   # visão contínua fala as mudanças
        self.brain = Brain(self.ctx)
        self.ctx.brain = self.brain
        self.dashboard = Dashboard(self.ctx, self)

        self.turn_lock = asyncio.Lock()
        self._confirm_lock = asyncio.Lock()   # serializa TTS+mic da confirmação
        self.safe_registry = None             # definido em run() (read+low)
        self.tasks.run_background = self._run_background

    # ------------------------------------------------------------------ #
    async def _confirm(self, question: str) -> bool:
        async with self._confirm_lock:           # evita 2 confirmações disputando o mic
            await self.mouth.say(question)
            await self.mouth.drain()
            answer = await self.ears.listen()
        if answer:
            self.bus.publish("speech", {"role": "you", "text": answer})
        return self.policy.parse_confirm(answer)

    async def _confirm_dashboard(self, question: str) -> bool:
        # painel não tem canal de confirmação por voz: recusa a ação de risco e avisa
        self.bus.publish("speech", {"role": "jarvis",
            "text": f"[recusado no painel — requer confirmação por voz] {question}"})
        return False

    async def _say_text_only(self, sentence: str) -> None:
        self.bus.publish("speech", {"role": "jarvis", "text": sentence})

    async def _run_background(self, prompt: str, tid: int) -> str:
        return await self.brain.oneshot(prompt, self.safe_registry, source="task")

    async def _run_routine(self, name: str, prompt: str) -> None:
        try:
            text = await self.brain.oneshot(prompt, self.safe_registry, source="routine")
        except Exception as e:  # noqa: BLE001
            text = f"(rotina falhou: {e})"
        await self.mouth.say(f"Rotina {name}. {text}")

    # ------------------------------------------------------------------ #
    async def handle_external(self, text: str) -> None:
        """Comando vindo do painel web."""
        speak_replies = self.cfg.get("dashboard", {}).get("speak_replies", False)
        speak = self.mouth.say if speak_replies else self._say_text_only
        async with self.turn_lock:
            await self.brain.run_turn(text, source="dashboard", speak=speak,
                                      confirm=self._confirm_dashboard)
        if speak_replies:
            await self.mouth.drain()

    # ------------------------------------------------------------------ #
    async def _event_consumer(self) -> None:
        q = self.bus.subscribe("*")
        while self.running:
            topic, data = await q.get()
            if topic == "task.done":
                res = (data.get("result") or "").strip()
                await self.mouth.say(f"Terminei a tarefa {data.get('label', '')}. {res[:400]}")
            elif topic == "routine.fired":
                asyncio.create_task(self._run_routine(data["name"], data["prompt"]))

    async def _voice_loop(self) -> None:
        while self.running:
            try:
                await self.ears.wait_for_trigger()
                user = await self.ears.listen()
            except (KeyboardInterrupt, asyncio.CancelledError):
                break
            if not user:
                continue
            self.bus.publish("speech", {"role": "you", "text": user})

            if user.lower().strip(" .!?") in EXIT_WORDS:
                await self.mouth.say("Encerrando. Até mais, Leo.")
                await self.mouth.drain()
                self.running = False
                break
            if self.policy.is_kill(user):
                self.policy.kill_flag = True   # interrompe também tarefas/planner de fundo
                await self.mouth.stop()
                await self.mouth.say("Parei.")
                continue

            async with self.turn_lock:
                try:
                    await self.brain.run_turn(user, source="voice", speak=self.mouth.say,
                                              confirm=self._confirm)
                except Exception as e:  # noqa: BLE001
                    await self.mouth.say(f"Deu um erro aqui: {e}")
            await self.mouth.drain()

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        self.mouth.start()
        await self.mcp.start()
        # registry depois do MCP (pra incluir as tools externas)
        self.ctx.registry = build_registry(self.ctx)
        # sub-agentes/rotinas/tarefas: leitura + baixo risco + shell/AppleScript
        # (os dinâmicos se auto-limitam a leitura quando não há confirmação)
        self.safe_registry = self.ctx.registry.subset(
            ("read", "low", "dynamic:shell", "dynamic:applescript"),
            exclude=AUTONOMOUS_DENY_TOOLS)   # fundo não reespawna agentes nem liga watch
        self.brain.ctx.registry = self.ctx.registry

        await self.dashboard.start()
        bg = [asyncio.create_task(self.scheduler.run()),
              asyncio.create_task(self._event_consumer())]

        n_tools = len(self.ctx.registry.specs)
        await self.mouth.say(
            f"Jarvis online, modo {self.ears.mode}, {n_tools} habilidades. "
            "Em que posso ajudar, Leo?")
        try:
            await self._voice_loop()
        finally:
            self.running = False
            self.ears.shutdown()          # libera o microfone do loop de wake word
            self.vision.shutdown()        # para a visão contínua
            for t in bg:
                t.cancel()
            await asyncio.gather(*bg, return_exceptions=True)
            await self.mcp.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis v2")
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    parser.add_argument("--text", action="store_true", help="modo texto")
    parser.add_argument("--mode", choices=["auto", "wake", "ptt", "text"], default=None)
    args = parser.parse_args()

    load_dotenv(os.path.join(HERE, ".env"))
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERRO: defina ANTHROPIC_API_KEY no .env (copie de .env.example).")
        sys.exit(1)

    cfg = load_config(args.config)
    if args.text:
        cfg.setdefault("voice", {})["input"] = "text"
    elif args.mode:
        cfg.setdefault("voice", {})["input"] = args.mode

    lg = cfg.get("logging", {})
    obs.setup_logging(_abs(lg.get("dir", "logs")), lg.get("level", "INFO"))
    assistant = Assistant(cfg)
    try:
        asyncio.run(assistant.run())
    except KeyboardInterrupt:
        print("\naté mais, Leo.")


if __name__ == "__main__":
    main()
