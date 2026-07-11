"""Padrões de resiliência: retry com backoff exponencial + jitter, e circuit breaker.

Sem dependências externas. Usado para blindar chamadas à API da Anthropic e a
serviços externos contra falhas transitórias (sobrecarga, rede, rate limit).
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable

from core.obs import get_logger, log
import logging

_log = get_logger("resilience")


def is_transient(exc: Exception) -> bool:
    """Heurística: erros que vale a pena repetir (sobrecarga/rede/5xx/rate limit).
    Marcadores específicos para evitar falsos positivos (ex.: 'rate' em 'generate')."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    markers = ("overloaded", "rate limit", "ratelimit", "timeout", "timed out",
               "connectionerror", "connection error", "connection reset",
               "temporarily", "service unavailable", "unavailable",
               "internal server error", "429", "529", "502", "503", "504",
               "apiconnection", "apitimeout")
    return any(m in name or m in text for m in markers)


async def with_retry(factory: Callable[[], Awaitable], *, attempts: int = 4,
                     base: float = 0.6, factor: float = 2.0, max_delay: float = 20.0,
                     retry_on: Callable[[Exception], bool] = is_transient,
                     label: str = "call"):
    """Executa `factory()` (que retorna um awaitable) com retry exponencial + jitter."""
    attempts = max(1, attempts)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt >= attempts or not retry_on(e):
                raise
            delay = min(max_delay, base * (factor ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.3)   # jitter
            log(_log, logging.WARNING, f"retry {label}",
                attempt=attempt, of=attempts, delay=round(delay, 2), err=str(e)[:160])
            await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


class CircuitOpen(Exception):
    """Levantada quando o circuito está aberto (serviço considerado indisponível)."""


class CircuitBreaker:
    """Fecha o circuito após N falhas seguidas; reabre para teste após cooldown."""

    def __init__(self, name: str, fail_threshold: int = 5, cooldown: float = 30.0):
        self.name = name
        self.fail_threshold = fail_threshold
        self.cooldown = cooldown
        self.fails = 0
        self.opened_at = 0.0
        self.state = "closed"   # closed | open | half_open

    def _now(self) -> float:
        return time.monotonic()

    async def call(self, factory: Callable[[], Awaitable]):
        if self.state == "open":
            if self._now() - self.opened_at >= self.cooldown:
                self.state = "half_open"
            else:
                raise CircuitOpen(f"circuito '{self.name}' aberto")
        try:
            result = await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.fails += 1
            if self.fails >= self.fail_threshold:
                self.state = "open"
                self.opened_at = self._now()
                log(_log, logging.ERROR, f"circuito '{self.name}' ABERTO",
                    fails=self.fails)
            raise
        # sucesso
        self.fails = 0
        self.state = "closed"
        return result
