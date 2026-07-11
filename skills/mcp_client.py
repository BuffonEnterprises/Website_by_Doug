"""Cliente MCP (Model Context Protocol) — pluga servidores externos como tools.

Cada servidor declarado em config `mcp_servers` sobe via stdio; suas tools
entram no registry com prefixo `mcp__<servidor>__<tool>`. É assim que o Jarvis
ganha Notion, Gmail, Slack, Google Drive/Calendar etc. sem código novo.

Toda a rede fica confinada aqui e é opcional: sem o pacote `mcp` instalado ou
sem servidores no config, o Jarvis roda normalmente sem nada disso.
"""
from __future__ import annotations

import asyncio

from skills.registry import ToolSpec


class MCPManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("mcp_servers", {}) or {}
        self.sessions: dict = {}
        self._stack = None
        self._tools: list[ToolSpec] = []

    async def start(self) -> None:
        if not self.cfg:
            return
        try:
            from contextlib import AsyncExitStack
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            print("[mcp] pacote 'mcp' não instalado — servidores MCP ignorados. "
                  "Instale com: pip install mcp")
            return

        self._stack = AsyncExitStack()
        for name, spec in self.cfg.items():
            try:
                params = StdioServerParameters(
                    command=spec["command"],
                    args=spec.get("args", []),
                    env=spec.get("env"),
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[name] = session
                await self._register_tools(name, session)
                print(f"[mcp] servidor '{name}' conectado.")
            except Exception as e:  # noqa: BLE001
                print(f"[mcp] falha ao conectar '{name}': {e}")

    async def _register_tools(self, server: str, session) -> None:
        listed = await session.list_tools()
        for t in listed.tools:
            full = f"mcp__{server}__{t.name}"
            schema = t.inputSchema or {"type": "object", "properties": {}}
            self._tools.append(ToolSpec(
                name=full,
                description=(t.description or f"Ferramenta {t.name} do servidor {server}")[:900],
                schema=schema,
                risk="medium",          # tools externas: confirma por padrão no safe_confirm
                handler=self._make_handler(server, t.name),
                describe=lambda a, s=server, tn=t.name: f"Vou usar {tn} no {s}.",
            ))

    def _make_handler(self, server: str, tool: str):
        async def handler(args: dict, ctx) -> str:
            session = self.sessions.get(server)
            if not session:
                return f"[erro] servidor MCP '{server}' indisponível"
            try:
                res = await asyncio.wait_for(session.call_tool(tool, args), timeout=90)
            except asyncio.TimeoutError:
                return f"[timeout] {tool} demorou demais"
            except Exception as e:  # noqa: BLE001
                return f"[erro mcp] {e}"
            chunks, has_nontext = [], False
            for c in getattr(res, "content", []) or []:
                text = getattr(c, "text", None)
                if text:
                    chunks.append(text)
                else:
                    has_nontext = True
            out = "\n".join(chunks) if chunks else (
                "[conteúdo não-textual retornado]" if has_nontext else "[ok]")
            if getattr(res, "isError", False):
                out = f"[erro mcp] {out}"
            return out[:8000] + ("\n[...truncado...]" if len(out) > 8000 else "")
        return handler

    def tool_specs(self) -> list[ToolSpec]:
        return self._tools

    async def stop(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001
                pass
