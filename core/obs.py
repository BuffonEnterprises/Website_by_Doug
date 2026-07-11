"""Observabilidade: logging estruturado (JSON) + correlation id.

Um logger só, com saída em JSON para `logs/jarvis.log` (rotativo) e console
em nível WARNING. Cada turno recebe um correlation id (contextvar) que aparece
em todas as linhas — dá pra rastrear uma interação inteira ponta a ponta.
Degrada com elegância: se o diretório de log não puder ser criado, cai só no console.
"""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import time

_cid: contextvars.ContextVar[str] = contextvars.ContextVar("cid", default="-")
_configured = False
_counter = 0


def new_cid(prefix: str = "t") -> str:
    global _counter
    _counter += 1
    cid = f"{prefix}{int(time.time()) % 100000}-{_counter}"
    _cid.set(cid)
    return cid


def set_cid(cid: str) -> None:
    _cid.set(cid)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "lvl": record.levelname,
            "logger": record.name,
            "cid": _cid.get(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in getattr(record, "extra_fields", {}).items():
            payload[k] = v
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_dir: str = "logs", level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("jarvis")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(console)

    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "jarvis.log"), maxBytes=5_000_000, backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(_JsonFormatter())
        root.addHandler(fh)
    except Exception:  # noqa: BLE001  (sem disco de log: segue só no console)
        pass
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"jarvis.{name}")


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """Log com campos estruturados extras."""
    logger.log(level, msg, extra={"extra_fields": fields})
