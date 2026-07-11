"""Planner — raciocínio multi-passo (plan → execute → reflect → synthesize).

Para objetivos complexos ("modo profundo"), decompõe em passos, executa cada um
com o loop de agente, verifica se o objetivo foi cumprido e sintetiza um resultado.
Roda em segundo plano (não trava a conversa) e avisa ao terminar.
"""
from __future__ import annotations

import re

from skills.registry import AUTONOMOUS_DENY_TOOLS, ToolSpec

_PLAN = ("Você é um planejador. Decomponha o OBJETIVO abaixo em no máximo 6 passos "
         "curtos, acionáveis e independentes, um por linha começando com '- '. "
         "Não execute nada agora, só liste os passos.\n\nOBJETIVO: {goal}")
_STEP = ("Execute este passo do objetivo maior '{goal}', usando ferramentas quando "
         "útil. Passo: {step}\nResponda com o resultado concreto do passo.")
_REFLECT = ("Objetivo: {goal}\nResultados dos passos:\n{results}\n\nO objetivo foi "
            "cumprido? Responda 'SIM' ou liste em uma frase o que ainda falta.")
_SYNTH = ("Sintetize um resultado final curto e natural (será falado em voz alta, em "
          "português) para o objetivo '{goal}', com base nos resultados:\n{results}")


def _parse_steps(text: str) -> list[str]:
    steps = []
    for line in (text or "").splitlines():
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if len(s) > 3:
            steps.append(s)
    return steps[:6]


class Planner:
    def __init__(self, ctx):
        self.ctx = ctx

    async def _run(self, goal: str) -> str:
        # exclui orquestração + watch -> passos NÃO reespawnam agentes nem ligam visão contínua
        ro = self.ctx.registry.subset(("read", "low"), exclude=AUTONOMOUS_DENY_TOOLS)
        ag = self.ctx.registry.subset(("read", "low", "dynamic:shell", "dynamic:applescript"),
                                      exclude=AUTONOMOUS_DENY_TOOLS)
        brain = self.ctx.brain

        plan = await brain.oneshot(_PLAN.format(goal=goal), ro, source="planner:plan")
        steps = _parse_steps(plan)
        if not steps:
            steps = [goal]

        results = []
        for i, step in enumerate(steps, 1):
            r = await brain.oneshot(_STEP.format(goal=goal, step=step), ag,
                                    source="planner:step")
            results.append(f"{i}. {step}\n   → {r}")
            if self.ctx.policy and self.ctx.policy.kill_flag:
                self.ctx.policy.kill_flag = False
                return "Modo profundo interrompido."

        joined = "\n".join(results)
        reflect = await brain.oneshot(_REFLECT.format(goal=goal, results=joined), ro,
                                      source="planner:reflect")
        # 1 rodada de replanejamento se não cumpriu
        if "sim" not in reflect.strip().lower()[:6]:
            extra = await brain.oneshot(
                f"Falta isto para o objetivo '{goal}': {reflect}. Resolva agora.",
                ag, source="planner:replan")
            results.append(f"(complemento) → {extra}")
            joined = "\n".join(results)

        return await brain.oneshot(_SYNTH.format(goal=goal, results=joined), ro,
                                   source="planner:synth")

    def tool_specs(self) -> list[ToolSpec]:
        async def deep_task(args, ctx):
            goal = args.get("goal", "")
            if not goal:
                return "[erro] informe o objetivo"
            tid = ctx.tasks.spawn_call(lambda: self._run(goal), f"profundo: {goal[:40]}")
            return (f"[ok] entrei em modo profundo (#{tid}): vou planejar, executar em "
                    f"passos e te dar o resultado. Trabalhando em segundo plano.")

        return [
            ToolSpec(
                "deep_task",
                "Modo profundo: para objetivos complexos e multi-etapa, planeja, executa "
                "passo a passo com autoverificação e sintetiza. Roda em segundo plano. "
                "Use quando o pedido exigir vários passos ou pesquisa+ação combinadas.",
                {"type": "object", "properties": {"goal": {"type": "string"}},
                 "required": ["goal"]},
                "low", deep_task,
            ),
        ]
