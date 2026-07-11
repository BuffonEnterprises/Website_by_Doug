"""Roteamento de modelo por complexidade estimada — custo/latência x capacidade.

Perguntas simples vão pro Haiku (barato/rápido); pedidos complexos ou "modo
profundo" vão pro Opus; o resto fica no Sonnet. Puramente heurístico e barato;
o objetivo é acertar na maioria e nunca degradar a resposta a ponto de errar.
"""
from __future__ import annotations

_HEAVY = ("planeje", "planejar", "arquitetura", "arquitete", "refatore", "refatora",
          "analise a fundo", "análise profunda", "profundo", "compare", "estratégia",
          "projete", "escreva o código", "implemente", "debug", "investigue", "audita",
          "auditoria", "raciocine", "passo a passo", "detalhado")
_SIMPLE = ("que horas", "qual", "quanto", "abre", "abra", "toca", "tocar", "pause",
           "próxima", "volume", "lembra", "anota", "hora", "olá", "oi", "obrigado")


class ModelRouter:
    def __init__(self, cfg: dict):
        llm = cfg.get("llm", {})
        r = cfg.get("routing", {})
        self.enabled = r.get("enabled", True)
        self.fast = r.get("fast_model", "claude-haiku-4-5")
        self.default = llm.get("model", "claude-sonnet-4-6")
        self.heavy = llm.get("heavy_model", "claude-opus-4-8")

    def pick(self, text: str, deep: bool = False) -> str:
        if deep:
            return self.heavy
        if not self.enabled:
            return self.default
        t = (text or "").lower().strip()
        if any(w in t for w in _HEAVY) or len(t) > 360:
            return self.heavy
        if len(t) <= 48 and any(w in t for w in _SIMPLE):
            return self.fast
        return self.default
