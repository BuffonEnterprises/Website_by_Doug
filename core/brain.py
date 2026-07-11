"""O cérebro: loop de agente assíncrono e streaming.

Diferenças-chave do v1:
  • Streaming token-a-token com TTS frase-a-frase (fala antes de terminar de pensar).
  • Política de risco de 4 níveis com confirmação por voz e denylist absoluta.
  • Ferramentas executadas em paralelo quando o modelo pede várias de uma vez.
  • Auto-recall: injeta memórias relevantes antes de responder.
  • Suporte multimodal (resultado de tool pode conter imagem — ex.: screenshot).
  • Contabilidade de tokens e auditoria de cada ação.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

from core.obs import get_logger, log, new_cid
from core.resilience import CircuitBreaker, is_transient, with_retry

_blog = get_logger("brain")
_SENTENCE_END = re.compile(r"([.!?…:;\n]+)")


class SentenceChunker:
    """Acumula deltas de texto e libera frases completas para a fala."""

    def __init__(self, min_len: int = 18):
        self.buf = ""
        self.min_len = min_len

    def push(self, text: str) -> list[str]:
        self.buf += text
        out, parts = [], _SENTENCE_END.split(self.buf)
        # parts alterna [texto, delim, texto, delim, ..., resto]
        rebuilt, i = "", 0
        sentences: list[str] = []
        while i < len(parts) - 1:
            chunk = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
            rebuilt += chunk
            sentences.append(chunk)
            i += 2
        remainder = parts[-1] if len(parts) % 2 == 1 else ""
        merged, cur = [], ""
        for s in sentences:
            cur += s
            if len(cur.strip()) >= self.min_len:
                merged.append(cur.strip())
                cur = ""
        self.buf = cur + remainder
        return [m for m in merged if m]

    def flush(self) -> list[str]:
        rest = self.buf.strip()
        self.buf = ""
        return [rest] if rest else []


class Brain:
    def __init__(self, ctx):
        from anthropic import AsyncAnthropic

        self.ctx = ctx
        self.cfg = ctx.cfg
        self.client = AsyncAnthropic()
        llm = self.cfg["llm"]
        self.model = llm["model"]
        self.max_tokens = int(llm["max_tokens"])
        self.temperature = float(llm["temperature"])
        self.max_iters = int(self.cfg["agent"]["max_tool_iterations"])
        self.history_turns = int(self.cfg["agent"].get("history_turns", 20))
        self.router = getattr(ctx, "router", None)
        self.state = getattr(ctx, "state", None)
        self.session_id = getattr(ctx, "session_id", "default")
        self.summary = ""
        self.breaker = CircuitBreaker("anthropic", fail_threshold=6, cooldown=30.0)
        self.messages: list[dict] = []
        if self.state:
            try:
                self.messages, self.summary = self.state.load(self.session_id)
            except Exception:  # noqa: BLE001
                self.messages, self.summary = [], ""

    # ------------------------------------------------------------------ #
    def _system(self, extra: str = "", override: str | None = None) -> list[dict]:
        now = datetime.now().strftime("%A, %d/%m/%Y %H:%M")
        base = (
            "Você é o Jarvis, assistente pessoal e segundo cérebro do Leo, rodando no Mac dele. "
            f"Agora: {now} (America/Sao_Paulo).\n\n"
            "FALE em português do Brasil, curto e natural — suas respostas são FALADAS. "
            "Nada de markdown/listas/textão. Uma a três frases, salvo se pedirem detalhe.\n\n"
            "Você controla o Mac de verdade: terminal, arquivos, AppleScript (Calendário, "
            "Lembretes, Notas, Mail, Mensagens, Spotify), visão de tela, web, memória de longo "
            "prazo, tarefas em segundo plano e rotinas agendadas. USE as ferramentas para AGIR — "
            "quando fizer sentido, chame VÁRIAS de uma vez. Não descreva o que faria; faça.\n\n"
            "Segurança: leitura e ações de baixo risco rodam sozinhas; o que muda estado passa "
            "por confirmação automática do sistema. NÃO peça permissão no texto — só chame a "
            "ferramenta. Se uma ação for negada ou recusada, siga em frente com elegância.\n\n"
            "Segundo cérebro: salve com memory_save o que valer lembrar (preferências, decisões, "
            "pessoas, projetos). Antes de dizer que não sabe algo pessoal, use memory_search. "
            "Se falhar algo, explique em uma frase e siga tentando resolver."
        )
        if override:
            blocks = [{"type": "text", "text": override}]
        else:
            blocks = [{"type": "text", "text": base}]
            if self.summary:
                blocks.append({"type": "text",
                               "text": f"[resumo da conversa anterior: {self.summary}]"})
        if extra:
            blocks.append({"type": "text", "text": extra})
        return blocks

    async def _auto_recall(self, user_text: str) -> str:
        if not self.cfg["memory"].get("auto_recall", True) or not self.ctx.memory:
            return ""
        try:
            hits = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.ctx.memory.search(user_text, 4))
        except Exception:  # noqa: BLE001
            return ""
        if not hits:
            return ""
        joined = " | ".join(h["texto"] for h in hits)
        return f"[memórias possivelmente relevantes: {joined}]"

    # ------------------------------------------------------------------ #
    async def oneshot(self, user_text: str, registry, source: str = "task",
                      speak=None, system_override=None) -> str:
        """Turno isolado (histórico próprio) — usado por rotinas, tarefas e sub-agentes."""
        return await self.run_turn(user_text, source=source, speak=speak,
                                   registry=registry, allow_confirm=False, messages=[],
                                   system_override=system_override)

    async def run_turn(self, user_text: str, source: str = "voice",
                       speak=None, registry=None, allow_confirm: bool = True,
                       messages=None, confirm=None, system_override=None) -> str:
        registry = registry or self.ctx.registry
        if messages is None:
            new_cid()
            await self._maybe_summarize()
        conv = self.messages if messages is None else messages
        conv.append({"role": "user", "content": user_text})
        # auto-recall só no fio principal — não injeta memórias do Leo em sub-agentes
        extra = await self._auto_recall(user_text) if messages is None else ""
        model = self.router.pick(user_text) if self.router else self.model
        last_text = ""

        for _ in range(self.max_iters):
            # só o fio principal consome/zera o kill (sub-agentes não roubam o sinal)
            if messages is None and self.ctx.policy and self.ctx.policy.kill_flag:
                self.ctx.policy.kill_flag = False
                if conv and conv[-1]["role"] == "user":
                    conv.append({"role": "assistant", "content": "Ok, parei tudo."})
                self._persist()
                return "Ok, parei tudo."

            spoken_here: list[str] = []
            emitted = {"any": False}

            async def emit(sentence: str):
                spoken_here.append(sentence)
                if speak:
                    await speak(sentence)

            async def do_stream():
                chunker = SentenceChunker()   # fresco a cada tentativa (retry seguro)
                async with self.client.messages.stream(
                    model=model, max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    system=self._system(extra, system_override),
                    tools=registry.api_tools(), messages=conv,
                ) as stream:
                    async for event in stream:
                        if (event.type == "content_block_delta"
                                and getattr(event.delta, "type", None) == "text_delta"):
                            emitted["any"] = True
                            for s in chunker.push(event.delta.text):
                                await emit(s)
                    fm = await stream.get_final_message()
                for s in chunker.flush():
                    await emit(s)
                return fm

            try:
                # só re-tenta se falhar ANTES de emitir texto (evita fala duplicada)
                final = await self.breaker.call(lambda: with_retry(
                    do_stream, attempts=3, label="stream",
                    retry_on=lambda e: is_transient(e) and not emitted["any"]))
            except Exception as e:  # noqa: BLE001
                log(_blog, logging.ERROR, "falha no streaming", err=str(e)[:200])
                msg = "Tive um problema pra falar com o modelo agora. Pode repetir?"
                if speak and not emitted["any"]:
                    await emit(msg)
                if conv and conv[-1]["role"] == "user":
                    conv.append({"role": "assistant", "content": msg})
                if messages is None:
                    self._persist()
                return msg

            if getattr(final, "usage", None) and self.ctx.audit:
                self.ctx.audit.log_llm(model, final.usage.input_tokens,
                                       final.usage.output_tokens)

            conv.append({"role": "assistant", "content": final.content})
            text_now = " ".join(b.text for b in final.content
                                if getattr(b, "type", None) == "text").strip()
            if text_now:
                last_text = text_now

            # executa QUALQUER bloco tool_use presente, mesmo se stop_reason='max_tokens'
            tool_uses = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                if messages is None:
                    self._persist()
                return last_text or "Feito."

            raw = await asyncio.gather(
                *[self._exec_tool(b, registry, source, allow_confirm, confirm)
                  for b in tool_uses],
                return_exceptions=True,
            )
            results = []
            for b, r in zip(tool_uses, raw):   # garante 1 tool_result por tool_use (sem órfão)
                if isinstance(r, Exception):
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": f"[erro interno: {r}]"})
                else:
                    results.append(r)
            conv.append({"role": "user", "content": results})

        fallback = last_text or "Parei por segurança (muitos passos). Pode reformular?"
        if conv and conv[-1]["role"] == "user":   # mantém alternância de papéis
            conv.append({"role": "assistant", "content": fallback})
        if messages is None:
            self._persist()
        return fallback

    # ------------------------------------------------------------------ #
    async def _exec_tool(self, block, registry, source: str,
                         allow_confirm: bool, confirm=None) -> dict:
        """NUNCA propaga exceção: sempre devolve um tool_result válido (par com o tool_use)."""
        name = getattr(block, "name", "?")
        tid = getattr(block, "id", "")
        try:
            args = dict(block.input or {})
        except Exception:  # noqa: BLE001
            args = {}
        t0 = time.time()

        def done(decision: str, content):
            if not isinstance(content, (str, list)):   # a API só aceita str ou lista de blocos
                content = "[ok]" if content is None else str(content)
            ms = int((time.time() - t0) * 1000)
            try:
                if self.ctx.audit:
                    self.ctx.audit.log_action(
                        source, name, args, decision,
                        content if isinstance(content, str) else "[multimodal]", ms)
            except Exception:  # noqa: BLE001  (auditoria nunca derruba a execução)
                pass
            return {"type": "tool_result", "tool_use_id": tid, "content": content}

        try:
            spec = registry.get(name)
            if spec is None:
                return done("error", f"[erro] ferramenta desconhecida: {name}")

            decision = self.ctx.policy.decide(spec.risk, args) if self.ctx.policy else "allow"
            if decision == "deny":
                return done("deny", "[negado pela política de segurança — ação bloqueada]")
            if decision == "confirm":
                cb = confirm or self.ctx.confirm
                if not allow_confirm or not cb:
                    return done("refused", "[precisava de confirmação e não havia canal — não executei]")
                try:
                    ok = await cb(spec.confirmation_text(args))
                except Exception as e:  # noqa: BLE001
                    return done("error", f"[erro na confirmação: {e}]")
                if not ok:
                    return done("refused", "[Leo não confirmou — ação cancelada]")

            result = await spec.call(args, self.ctx)
            return done("confirm_yes" if decision == "confirm" else "allow", result)
        except Exception as e:  # noqa: BLE001
            return done("error", f"[erro ao executar {name}: {e}]")

    # ------------------------------------------------------------------ #
    def _persist(self) -> None:
        self._strip_history_images()
        if not self.state:
            return
        try:
            self.state.save(self.session_id, self.messages, self.summary)
        except Exception:  # noqa: BLE001
            pass

    def _strip_history_images(self) -> None:
        """Remove imagens (screenshots) do histórico depois de consumidas — evita
        re-envio caro a cada turno e inchaço do sessions.db. A análise textual do
        modelo permanece; a imagem só existe durante o turno em que é capturada."""
        for m in self.messages:
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if (isinstance(b, dict) and b.get("type") == "tool_result"
                        and isinstance(b.get("content"), list)):
                    b["content"] = [
                        {"type": "text", "text": "[imagem removida do histórico]"}
                        if isinstance(x, dict) and x.get("type") == "image" else x
                        for x in b["content"]
                    ]

    @staticmethod
    def _messages_to_text(messages) -> str:
        parts = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, str):
                parts.append(f"{m['role']}: {c}")
            elif isinstance(c, list):
                for b in c:
                    t = b.get("text") if isinstance(b, dict) else getattr(b, "text", None)
                    if t:
                        parts.append(f"{m['role']}: {t}")
        return "\n".join(parts)

    async def _maybe_summarize(self) -> None:
        """Resumo rolante: condensa turnos antigos em self.summary e os descarta —
        contexto praticamente ilimitado com janela limitada."""
        starts = [i for i, m in enumerate(self.messages)
                  if m["role"] == "user" and isinstance(m["content"], str)]
        if len(starts) <= self.history_turns:
            return
        cut = starts[len(starts) - self.history_turns]
        old = self.messages[:cut]
        if not old:
            return
        model = self.router.fast if self.router else self.model
        try:
            resp = await with_retry(lambda: self.client.messages.create(
                model=model, max_tokens=400,
                system="Resuma em português, curto, os fatos, preferências e decisões "
                       "salientes desta parte da conversa (para memória de longo prazo).",
                messages=[{"role": "user", "content": self._messages_to_text(old)[:6000]}]),
                attempts=3, label="summarize")
            s = "".join(b.text for b in resp.content
                        if getattr(b, "type", None) == "text").strip()
            if s:
                self.summary = (self.summary + " " + s).strip()[-2000:]
            self.messages = self.messages[cut:]   # só descarta o antigo se resumiu com êxito
        except Exception:  # noqa: BLE001
            pass   # falhou (ex.: overload): mantém o histórico, sem amnésia silenciosa
