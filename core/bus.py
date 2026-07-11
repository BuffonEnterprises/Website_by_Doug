"""Event bus mínimo (pub/sub asyncio) — desacopla os subsistemas.

Tópicos usados: speech, chat, audit, task.spawn, task.done, routine.fired,
status. Assinantes de "*" recebem tudo (é o que o dashboard usa).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict


class Bus:
    def __init__(self) -> None:
        self._topics: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, topic: str = "*") -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._topics[topic].append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        for subs in self._topics.values():
            if q in subs:
                subs.remove(q)

    def publish(self, topic: str, data: dict) -> None:
        for q in list(self._topics.get(topic, [])) + list(self._topics.get("*", [])):
            try:
                q.put_nowait((topic, data))
            except asyncio.QueueFull:  # assinante lento: descarta o mais antigo
                try:
                    q.get_nowait()
                    q.put_nowait((topic, data))
                except Exception:  # noqa: BLE001
                    pass
