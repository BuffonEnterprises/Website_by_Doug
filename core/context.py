"""Contexto compartilhado — o "service locator" que as tools recebem.

Evita imports circulares e passa tudo que um handler possa precisar (config,
memória, auditoria, política, subsistemas) num objeto só.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class Context:
    cfg: dict
    bus: Any
    memory: Any = None
    audit: Any = None
    policy: Any = None
    tasks: Any = None
    scheduler: Any = None
    mcp: Any = None
    registry: Any = None
    confirm: Optional[Callable] = None      # async (pergunta:str) -> bool
    # v3
    router: Any = None
    state: Any = None
    knowledge: Any = None
    planner: Any = None
    vision: Any = None
    brain: Any = None
    session_id: str = "default"
