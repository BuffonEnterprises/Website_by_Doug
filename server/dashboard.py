"""Painel local (FastAPI + WebSocket) — veja e comande o Jarvis pelo navegador.

Mostra em tempo real o que ele fala, cada ação/decisão de política e as tarefas
em segundo plano. Também dá pra digitar comandos. Tudo em 127.0.0.1 (local).
Opcional: sem FastAPI/uvicorn instalados, o Jarvis roda sem o painel.
"""
from __future__ import annotations

import asyncio
import json

# Importados no nível do módulo para que as anotações de tipo string dos
# endpoints (efeito de `from __future__ import annotations`) sejam resolvíveis
# por FastAPI via get_type_hints (que só olha os globals do módulo). Sem isto,
# `req: "Request"` fica um forward-ref não resolvido e o FastAPI trata `req`
# como query param. Guardado: o painel é opcional (roda sem FastAPI).
try:
    from fastapi import Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
except ImportError:  # pragma: no cover - painel apenas opcional
    Request = WebSocket = WebSocketDisconnect = HTMLResponse = None

PAGE = """<!doctype html><html lang=pt-br><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Jarvis</title><style>
:root{--bg:#0b0f14;--fg:#e6edf3;--mut:#8b98a5;--acc:#3fb950;--warn:#d29922;--dang:#f85149;--card:#131a22}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 20px;border-bottom:1px solid #21262d;display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--acc);box-shadow:0 0 8px var(--acc)}
h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
main{display:grid;grid-template-columns:1fr 340px;gap:14px;padding:14px;max-width:1100px;margin:0 auto}
#log{height:64vh;overflow:auto;background:var(--card);border:1px solid #21262d;border-radius:10px;padding:14px}
.msg{margin:6px 0;padding:8px 10px;border-radius:8px;background:#0f1620}
.you{border-left:3px solid #58a6ff}.jarvis{border-left:3px solid var(--acc)}
.side{background:var(--card);border:1px solid #21262d;border-radius:10px;padding:14px;height:64vh;overflow:auto}
.act{font-size:12px;color:var(--mut);margin:4px 0;font-family:ui-monospace,monospace}
.allow{color:var(--acc)}.confirm_yes{color:var(--warn)}.deny,.refused{color:var(--dang)}
form{display:flex;gap:8px;grid-column:1/-1;max-width:1100px;margin:0 auto;width:100%}
input{flex:1;background:var(--card);border:1px solid #30363d;color:var(--fg);padding:10px 12px;border-radius:8px}
button{background:var(--acc);border:0;color:#04260f;font-weight:700;padding:10px 16px;border-radius:8px;cursor:pointer}
h2{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--mut);margin:0 0 8px}
</style></head><body>
<header><span class=dot></span><h1>JARVIS — painel local</h1></header>
<main>
<div id=log></div>
<div class=side><h2>Atividade</h2><div id=acts></div><h2 style=margin-top:16px>Tarefas</h2><div id=tasks></div></div>
<form id=f><input id=t placeholder="Digite um comando para o Jarvis…" autocomplete=off>
<button>Enviar</button></form>
</main><script>
const log=document.getElementById('log'),acts=document.getElementById('acts'),tasks=document.getElementById('tasks');
function add(el,html,cls){const d=document.createElement('div');d.className=cls;d.innerHTML=html;el.appendChild(d);el.scrollTop=el.scrollHeight}
const ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
ws.onmessage=e=>{const m=JSON.parse(e.data),d=m.data||{};
 if(m.topic==='speech')add(log,'<b>'+(d.role==='jarvis'?'Jarvis':'Você')+':</b> '+esc(d.text),'msg '+(d.role==='jarvis'?'jarvis':'you'));
 else if(m.topic==='chat')add(log,'<b>Você:</b> '+esc(d.text),'msg you');
 else if(m.topic==='audit')add(acts,'<span class="'+d.decision+'">'+d.decision+'</span> · '+esc(d.tool)+' · '+d.ms+'ms','act');
 else if(m.topic==='task.start')add(tasks,'▶ #'+d.id+' '+esc(d.label),'act');
 else if(m.topic==='task.done')add(tasks,'✓ #'+d.id+' '+esc(d.label)+' ('+d.status+')','act');
};
function esc(s){return (s||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}
document.getElementById('f').onsubmit=e=>{e.preventDefault();const t=document.getElementById('t');
 if(!t.value.trim())return;fetch('/say',{method:'POST',headers:{'content-type':'application/json'},
 body:JSON.stringify({text:t.value})});t.value=''};
</script></body></html>"""


class Dashboard:
    def __init__(self, ctx, assistant):
        self.ctx = ctx
        self.assistant = assistant
        self.cfg = ctx.cfg.get("dashboard", {})
        self._server = None

    async def start(self) -> None:
        if not self.cfg.get("enabled", True):
            return
        try:
            import uvicorn
            from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
            from fastapi.responses import HTMLResponse
        except ImportError:
            print("[painel] FastAPI/uvicorn não instalados — painel desativado.")
            return

        app = FastAPI()

        @app.get("/")
        async def index() -> "HTMLResponse":
            return HTMLResponse(PAGE)

        @app.post("/say")
        async def say(req: "Request"):
            try:
                body = await req.json()
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": "json inválido"}
            text = ((body or {}).get("text") or "").strip()
            if text:
                self.ctx.bus.publish("chat", {"text": text})
                asyncio.create_task(self.assistant.handle_external(text))
            return {"ok": True}

        @app.get("/health")
        async def health():
            reg = getattr(self.ctx, "registry", None)
            return {"status": "ok",
                    "tools": len(reg.specs) if reg else 0,
                    "mode": self.ctx.cfg.get("safety", {}).get("mode"),
                    "model": self.ctx.cfg.get("llm", {}).get("model")}

        @app.get("/metrics")
        async def metrics():
            au = getattr(self.ctx, "audit", None)
            tasks = getattr(self.ctx, "tasks", None)
            return {"summary": au.summary(24) if au else "",
                    "recent": au.recent(20) if au else [],
                    "tasks": tasks.status() if tasks else ""}

        @app.websocket("/ws")
        async def ws(sock: "WebSocket"):
            await sock.accept()
            q = self.ctx.bus.subscribe("*")
            try:
                while True:
                    topic, data = await q.get()
                    await sock.send_text(json.dumps({"topic": topic, "data": data}))
            except Exception:  # noqa: BLE001  (WebSocketDisconnect etc.)
                pass
            finally:
                self.ctx.bus.unsubscribe(q)

        config = uvicorn.Config(app, host=self.cfg.get("host", "127.0.0.1"),
                                port=int(self.cfg.get("port", 8737)),
                                log_level="warning")
        self._server = uvicorn.Server(config)
        asyncio.create_task(self._server.serve())
        print(f"[painel] http://{self.cfg.get('host', '127.0.0.1')}:{self.cfg.get('port', 8737)}")
