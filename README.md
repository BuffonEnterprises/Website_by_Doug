# 🧠 JARVIS v3 — plataforma de agente pessoal por voz

De assistente a **plataforma de agente**. Você fala (wake word "hey jarvis"), ele
pensa com o Claude, **responde falando em streaming**, controla o Mac inteiro, **enxerga
sua tela**, **lembra de tudo** (memória semântica + resumo rolante persistente),
**delega a sub-agentes**, **planeja tarefas complexas em passos**, **conhece seus
arquivos** (RAG), roda **rotinas agendadas** e **tarefas em segundo plano**, conecta seus
apps via **MCP**, e tudo sob uma **política de segurança de 4 níveis** com auditoria,
**resiliência** (retry + circuit breaker) e **logging estruturado**. Pode rodar como
**serviço always-on**.

---

## Novidades do v3 (sobre o v2)

| Eixo | v3 traz |
|---|---|
| **Inteligência** | **Planner** (plan→executa→reflete, `deep_task`), **sub-agentes** especializados (`spawn_agent`: pesquisador/coder/sysadmin/analista), **roteamento de modelo** por complexidade (Haiku/Sonnet/Opus) |
| **Memória** | **Resumo rolante** persistente (contexto ~ilimitado), **persistência de sessão** (não é mais amnésico ao reiniciar), **importância** + reflexão (`memory_reflect`) |
| **Conhecimento** | **RAG dos seus arquivos** (`knowledge_index`/`knowledge_search`) — o Jarvis passa a conhecer seus projetos |
| **Robustez** | **Retry com backoff+jitter**, **circuit breaker** na API, **logging estruturado** (JSON, correlation id), streaming que se recupera de falha transitória |
| **Extensibilidade** | **Plugins auto-descobertos** (`skills/plugins/*.py`), health/métricas no painel |
| **Ops** | **Serviço always-on** (launchd) — sobe no login e se reergue sozinho |

---

## Arquitetura

```
                         ┌──────────── EVENT BUS (asyncio pub/sub) ────────────┐
   você fala             │                                                     │
      │ 🎙️  EARS         ▼                                                     │
      ▼  wake+STT   ┌───────────┐  roteia modelo   ┌─────────────────────────┐ │
  ┌────────┐  texto │  BRAIN    │─────────────────▶│  REGISTRY (tools)       │ │
  │ ROUTER │───────▶│ streaming │  tools ∥ paralelo│  mac·web·memória·visão  │ │
  └────────┘        │ +resiliênc│◀─────────────────│  knowledge·mcp·plugins  │ │
      ▲             │ +resumo   │   tool_result    │  ▲ planner  ▲ agents    │ │
  ┌────────┐ frases │  rolante  │                  └──┼──────────┼───────────┘ │
  │ MOUTH  │◀───────└─────┬─────┘                     │ deep_task│ spawn_agent │
  │say/11L │             │                            ▼          ▼             │
  └────────┘        ┌─────┴──────┐   ┌──────────┐  (sub-agentes e planos em    │
      │  POLICY(4níveis)+AUDIT   │   │ SCHEDULER│   segundo plano, isolados)   │
      ▼   +RESILIENCE +LOGGING   │   │ rotinas  │   ┌──────────────────────────┴┐
   STATE (sessões+resumo, SQLite)│   └──────────┘   │  DASHBOARD /health /metrics│
   KNOWLEDGE (RAG, SQLite)       │                  └───────────────────────────┘
```

Pastas: `core/` (brain, policy, audit, scheduler, tasks, bus, config, context, **router,
state, planner, resilience, obs**) · `senses/` (ears, mouth, **ultravision**) · `skills/` (registry,
mac, web, memory, mcp_client, **knowledge, agents, plugins/**) · `server/` (dashboard) ·
`service/` (install_service.sh).

---

## Instalação

```bash
cd "jarvis"
brew install portaudio
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # nada de novo além do v2
cp .env.example .env && open -e .env    # ANTHROPIC_API_KEY
./run.sh
```

Permissões do macOS (Ajustes → Privacidade e Segurança): **Microfone**, **Automação**,
**Acessibilidade**, **Gravação de Tela** (visão), e opcional **Acesso Total ao Disco**.

### Serviço always-on (opcional)

```bash
bash service/install_service.sh     # sobe no login, escuta o wake word, se reergue sozinho
# parar:  launchctl unload ~/Library/LaunchAgents/com.leo.jarvis.plist
```

---

## Uso

```bash
./run.sh            # voz (wake word "hey jarvis", ou Enter se indisponível)
./run.sh --mode ptt # push-to-talk
./run.sh --text     # digitar
```

Painel ao vivo: **http://127.0.0.1:8737** (fala, decisões de segurança, tarefas; `/health`, `/metrics`).

### Exemplos (v3)

- "Hey Jarvis, entra em **modo profundo**: audita a segurança do meu projeto X e me dá um plano." → `deep_task` planeja e executa em passos
- "Manda o **pesquisador** ver as novidades do Postgres 17 e o **coder** analisar meu módulo de auth." → dois sub-agentes em paralelo
- "**Indexa** minha pasta ~/Projetos/apex e me diz como funciona a autenticação." → RAG
- "Todo dia útil às 8h, briefing da agenda + notícias." → rotina agendada
- "O que você fez na última hora?" → auditoria · "para tudo" → aborta tudo (inclusive tarefas de fundo)

---

## Segurança (4 níveis + resiliência)

`read` (livre) · `low` (livre) · `medium` (confirma) · `high` (confirma). Shell e AppleScript
são classificados dinamicamente; há **denylist absoluta** (nunca roda) e **guardas de caminho**
catastrófico. Sub-agentes, rotinas e tarefas de fundo rodam **sem confirmação → só leitura e
baixo risco**, e **não podem reespawnar agentes** (anti-cascata). Modos: `always_confirm`,
`safe_confirm` (padrão), `autonomous`, `yolo`. Tudo auditado; chamadas à API têm retry + circuit
breaker; logs em `logs/jarvis.log`.

---

## Qualidade

Revisado por **10 agentes adversariais** em duas rodadas (streaming/async, áudio, red-team de
segurança, skills/MCP/memória, wiring/concorrência, estado/serialização, planner/agentes,
conhecimento/memória, ops/plugins). Todos os bugs confirmados corrigidos — incluindo tool_use
órfão (400), resample de STT, barge-in, tarefas de fundo mortas, locks SQLite cross-thread,
~10 bypasses de segurança, bloat de imagem no histórico, fork-bomb de agentes, DoS no `calc`,
colisão de plugin e sumarização com perda de contexto. **Bateria offline: 40/40.**

## Roteiro (v4)

Barge-in cortando a fala em tempo real; visão contínua (assistir a tela e agir); memória
episódica com grafo de entidades; execução distribuída (o mesmo cérebro no Mac, iPhone e web);
fila durável de eventos. Bora. 🚀
