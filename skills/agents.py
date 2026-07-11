"""Sub-agentes especializados — delegação e paralelismo.

O Jarvis principal pode "chamar um especialista" (spawn_agent): um agente com
persona e conjunto de ferramentas focados, que roda em segundo plano e reporta.
Por segurança, sub-agentes rodam SEM confirmação — logo só executam leitura e
ações de baixo risco (comandos de shell/AppleScript que mudem estado são
auto-recusados). Eles investigam e PROPÕEM; o Jarvis principal executa o arriscado.
"""
from __future__ import annotations

from skills.registry import ToolSpec

_AGENT_RISKS = ("read", "low", "dynamic:shell", "dynamic:applescript")

PERSONAS: dict[str, dict] = {
    "pesquisador": {
        "tools": {"web_search", "web_fetch", "memory_search", "memory_recent",
                  "knowledge_search", "activity_report"},
        "system": ("Você é um pesquisador meticuloso. Busque em várias fontes, leia, "
                   "cruze informações e devolva um resumo objetivo em português, "
                   "citando as fontes (URLs). Seja conciso e factual."),
    },
    "coder": {
        "tools": {"read_file", "list_dir", "run_shell", "knowledge_search", "memory_search"},
        "system": ("Você é um engenheiro de software sênior. Leia o código (somente "
                   "leitura), entenda a arquitetura e proponha a solução ou o patch como "
                   "TEXTO (diffs/trechos). Você NÃO altera arquivos — apenas analisa e "
                   "propõe; o Jarvis principal aplica com confirmação. Seja preciso."),
    },
    "sysadmin": {
        "tools": {"run_shell", "read_file", "list_dir", "activity_report"},
        "system": ("Você é um SRE/sysadmin. Inspecione o sistema apenas por leitura "
                   "(ls, ps, df, git status, logs) e diagnostique o problema com um "
                   "plano de ação claro. Não execute nada destrutivo."),
    },
    "analista": {
        "tools": {"memory_search", "knowledge_search", "web_search", "web_fetch", "read_file"},
        "system": ("Você é um analista. Sintetize, compare alternativas, pese trade-offs "
                   "e conclua com uma recomendação fundamentada, em português."),
    },
}


def tool_specs() -> list[ToolSpec]:
    async def spawn_agent(args, ctx):
        persona = (args.get("persona") or "pesquisador").lower().strip()
        task = args.get("task", "")
        p = PERSONAS.get(persona)
        if not p:
            return f"[erro] persona desconhecida. Use: {', '.join(PERSONAS)}"
        if not (getattr(ctx, "brain", None) and ctx.tasks and ctx.registry):
            return "[erro] subsistema de agentes indisponível"
        reg = ctx.registry.subset(_AGENT_RISKS, names=p["tools"])

        async def run():
            return await ctx.brain.oneshot(task, reg, source=f"agent:{persona}",
                                           system_override=p["system"])

        tid = ctx.tasks.spawn_call(run, f"{persona}: {task[:40]}")
        return (f"[ok] acionei o agente '{persona}' (#{tid}) em segundo plano; "
                f"te aviso quando ele terminar.")

    return [
        ToolSpec(
            "spawn_agent",
            "Delega uma tarefa a um sub-agente especializado que roda em paralelo e "
            "reporta ao terminar. Personas: pesquisador, coder, sysadmin, analista. "
            "Use para trabalho focado/demorado (pesquisa a fundo, análise de código, "
            "diagnóstico). Sub-agentes só leem/agem em baixo risco.",
            {"type": "object", "properties": {
                "persona": {"type": "string",
                            "enum": list(PERSONAS.keys())},
                "task": {"type": "string"}},
             "required": ["persona", "task"]},
            "low", spawn_agent,
        ),
    ]
