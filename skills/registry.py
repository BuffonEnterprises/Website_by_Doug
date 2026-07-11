"""Registro central de ferramentas.

Cada skill exporta uma lista de ToolSpec; o registry agrega tudo (incluindo
tools dinâmicas de servidores MCP), gera o JSON para a API da Anthropic
(com prompt caching) e despacha as chamadas — handlers síncronos rodam
num executor para não travar o event loop.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any, Callable

# Ferramentas de orquestração — NÃO devem ser expostas a sub-agentes/rotinas/tarefas
# de fundo, senão um agente pode reespawnar agentes em cascata (fork-bomb).
ORCHESTRATION_TOOLS = {"deep_task", "spawn_agent", "spawn_task"}
# Além da orquestração, a visão contínua (fala sozinha e gasta tokens) também não
# deve ser ligável por contexto autônomo (rotina/tarefa de fundo/planner).
AUTONOMOUS_DENY_TOOLS = ORCHESTRATION_TOOLS | {"vision_watch_start", "vision_watch_stop"}


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    risk: str                      # read|low|medium|high|dynamic:shell|dynamic:applescript
    handler: Callable              # (args: dict, ctx) -> str | list[block]
    describe: Callable | None = field(default=None)  # (args) -> frase de confirmação

    def api(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema}

    async def call(self, args: dict, ctx) -> Any:
        if asyncio.iscoroutinefunction(self.handler):
            return await self.handler(args, ctx)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.handler(args, ctx))

    def confirmation_text(self, args: dict) -> str:
        if self.describe:
            try:
                return self.describe(args)
            except Exception:  # noqa: BLE001
                pass
        return f"Vou executar {self.name}."


class Registry:
    def __init__(self, specs: list[ToolSpec]):
        seen, deduped = set(), []
        for s in specs:   # nomes de tool DEVEM ser únicos (a API rejeita duplicados)
            if s.name in seen:
                print(f"[registry] nome de tool duplicado ignorado: {s.name}")
                continue
            seen.add(s.name)
            deduped.append(s)
        self.specs = deduped
        self._by_name = {s.name: s for s in deduped}
        self._api_tools = [copy.deepcopy(s.api()) for s in deduped]
        if self._api_tools:  # prompt caching: marca o último bloco de tools
            self._api_tools[-1]["cache_control"] = {"type": "ephemeral"}

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    def api_tools(self) -> list[dict]:
        return self._api_tools

    def subset(self, risks: tuple[str, ...] = ("read", "low"), names=None,
               exclude=None) -> "Registry":
        ex = exclude or set()
        return Registry([s for s in self.specs
                         if s.risk in risks and (names is None or s.name in names)
                         and s.name not in ex])


def build_registry(ctx) -> Registry:
    """Agrega as ferramentas de todas as skills + MCP."""
    from skills import mac, memory as memskill, web

    specs: list[ToolSpec] = []
    specs += mac.tool_specs()
    specs += web.tool_specs()
    specs += memskill.tool_specs()
    if getattr(ctx, "vision", None):        # Ultravision (substitui o antigo screen_capture)
        specs += ctx.vision.tool_specs()
    specs += _control_specs()
    if ctx.tasks:
        specs += ctx.tasks.tool_specs()
    if ctx.scheduler:
        specs += ctx.scheduler.tool_specs()
    if ctx.mcp:
        specs += ctx.mcp.tool_specs()
    if getattr(ctx, "knowledge", None):
        specs += ctx.knowledge.tool_specs()
    if getattr(ctx, "planner", None):
        specs += ctx.planner.tool_specs()
    from skills import agents
    specs += agents.tool_specs()
    specs += _load_plugins(ctx)
    return Registry(specs)


def _load_plugins(ctx) -> list[ToolSpec]:
    """Auto-descobre plugins em skills/plugins/*.py que exponham tool_specs(ctx)."""
    import glob
    import importlib
    import os

    out: list[ToolSpec] = []
    plugdir = os.path.join(os.path.dirname(__file__), "plugins")
    if not os.path.isdir(plugdir):
        return out
    for path in sorted(glob.glob(os.path.join(plugdir, "*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"skills.plugins.{name}")
            fn = getattr(mod, "tool_specs", None)
            if callable(fn):
                out += fn(ctx)
        except Exception as e:  # noqa: BLE001
            print(f"[plugin] falha ao carregar '{name}': {e}")
    return out


def _control_specs() -> list[ToolSpec]:
    def activity_report(args: dict, ctx) -> str:
        return ctx.audit.summary(int(args.get("hours", 24)))

    return [
        ToolSpec(
            name="activity_report",
            description="Relatório do que o Jarvis fez (ações, decisões e tokens) "
                        "nas últimas N horas. Use quando o Leo perguntar o que você fez.",
            schema={"type": "object",
                    "properties": {"hours": {"type": "integer", "description": "padrão 24"}},
                    "required": []},
            risk="read",
            handler=activity_report,
        ),
    ]
