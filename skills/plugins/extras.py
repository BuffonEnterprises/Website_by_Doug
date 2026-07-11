"""Plugin de exemplo — o contrato de extensibilidade do Jarvis.

Para adicionar habilidades sem tocar no núcleo, crie um arquivo em
skills/plugins/*.py que exponha `tool_specs(ctx) -> list[ToolSpec]`. O Jarvis
carrega automaticamente no boot. (Arquivos começando com '_' são ignorados.)
"""
from __future__ import annotations

import ast
import operator
import uuid

from skills.registry import ToolSpec

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
        ast.USub: operator.neg, ast.UAdd: operator.pos, ast.FloorDiv: operator.floordiv}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 128 or abs(left) > 1_000_000):
            raise ValueError("expoente/base grande demais")   # evita OOM/travar o loop
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("expressão não permitida")


def _calc(expr: str) -> str:
    try:
        return str(_eval(ast.parse(expr, mode="eval").body))
    except Exception as e:  # noqa: BLE001
        return f"[erro] expressão inválida: {e}"


def tool_specs(ctx) -> list[ToolSpec]:
    return [
        ToolSpec("calc", "Calculadora aritmética segura (ex.: '2*(3+4)/5').",
                 {"type": "object", "properties": {"expr": {"type": "string"}},
                  "required": ["expr"]},
                 "read", lambda a, c: _calc(a.get("expr", ""))),
        ToolSpec("uuid", "Gera um identificador único (UUID v4).",
                 {"type": "object", "properties": {}, "required": []},
                 "read", lambda a, c: str(uuid.uuid4())),
    ]
