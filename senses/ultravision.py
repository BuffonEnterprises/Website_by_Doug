"""Ultravision — visão avançada do Jarvis.

Muito além de um screenshot: captura por monitor, por região e por janela em
foco; OCR do que está na tela (ocrmac → pytesseract → fallback multimodal); e
VISÃO CONTÍNUA (watch) que observa a tela em segundo plano e avisa/descreve
quando algo muda, via hash perceptual (sem reprocessar quadros idênticos).

Tudo degrada com elegância: sem Pillow, sem OCR local, sem macOS — cada tool
devolve uma mensagem clara em vez de quebrar.
"""
from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import tempfile
import time

from skills import mac as _mac
from skills.registry import ToolSpec


# ---------------------------------------------------------------------------
# Captura
# ---------------------------------------------------------------------------

def _capture(display: int | None = None, region: str | None = None,
             timeout: int = 15) -> bytes | None:
    fd, path = tempfile.mkstemp(prefix="jarvis_uv_", suffix=".png")
    os.close(fd)
    try:
        cmd = ["screencapture", "-x", "-t", "png"]
        if display:
            cmd += ["-D", str(int(display))]
        if region:
            cmd += ["-R", region]
        cmd.append(path)
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0 or not os.path.isfile(path) or os.path.getsize(path) == 0:
            return None
        with open(path, "rb") as f:
            return f.read()
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _frontmost_region() -> str | None:
    """Bounds 'x,y,w,h' da janela em foco (requer Acessibilidade). None se falhar."""
    script = (
        'tell application "System Events" to tell (first application process '
        'whose frontmost is true) to tell front window to return '
        '(value of attribute "AXPosition") & "," & (value of attribute "AXSize")'
    )
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or "").strip()
    parts = [p.strip() for p in out.split(",") if p.strip()]
    if len(parts) == 4:
        try:   # AXPosition/AXSize às vezes vêm como float ("100.0")
            return ",".join(str(int(float(p))) for p in parts)
        except ValueError:
            return None
    return None


def _count_displays() -> int:
    try:
        out = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                             capture_output=True, text=True, timeout=12).stdout
        return max(1, out.count("Resolution:"))
    except Exception:  # noqa: BLE001
        return 1


# ---------------------------------------------------------------------------
# Processamento de imagem (Pillow opcional)
# ---------------------------------------------------------------------------

def _downscale(png: bytes, max_w: int = 1400) -> tuple[bytes, str]:
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=72)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        return png, "image/png"


def _image_blocks(png: bytes, note: str):
    data, media = _downscale(png)
    if media == "image/png" and len(data) > 3_400_000:   # ~4.5MB após base64 (limite da API)
        return ("[erro] imagem grande demais e Pillow indisponível para reduzir. "
                "Instale com: pip install Pillow")
    b64 = base64.b64encode(data).decode()
    return [
        {"type": "text", "text": note},
        {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
    ]


def _ahash(png: bytes) -> int | None:
    """Hash perceptual 64-bit (average hash) — para detectar mudança de tela."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png)).convert("L").resize((8, 8))
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for i, p in enumerate(pixels):
            if p > avg:
                bits |= (1 << i)
        return bits
    except Exception:  # noqa: BLE001
        return None


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _ocr(png: bytes) -> str | None:
    """OCR local: ocrmac (macOS Vision) → pytesseract → None (usa fallback multimodal)."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png))
    except Exception:  # noqa: BLE001
        return None
    try:
        from ocrmac import ocrmac
        res = ocrmac.OCR(img).recognize()
        text = "\n".join(r[0] for r in res if r and r[0])
        return text.strip() or "(nenhum texto reconhecido)"
    except Exception:  # noqa: BLE001
        pass
    try:
        import pytesseract
        return (pytesseract.image_to_string(img) or "").strip() or "(nenhum texto reconhecido)"
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Subsistema
# ---------------------------------------------------------------------------

class UltraVision:
    def __init__(self, ctx):
        self.ctx = ctx
        self.speak = None                 # injetado pelo Assistant (mouth.say)
        self._watch_task: asyncio.Task | None = None
        self._watch_stop = asyncio.Event()
        self._prev_hash: int | None = None

    # -------- captura resolvida (display/region/window) --------
    def _resolve_and_capture(self, args: dict) -> bytes | None:
        region = None
        if args.get("window"):
            region = _frontmost_region()
        elif all(k in args for k in ("x", "y", "w", "h")):
            try:
                region = f"{int(args['x'])},{int(args['y'])},{int(args['w'])},{int(args['h'])}"
            except (TypeError, ValueError):
                region = None
        try:
            display = int(args["display"]) if args.get("display") else None
        except (TypeError, ValueError):
            display = None
        return _capture(display=display, region=region)

    # -------- descrição via modelo (usado pelo watch) --------
    async def _describe(self, png: bytes) -> str:
        blocks = _image_blocks(png, "")
        if not isinstance(blocks, list):
            return "(sem imagem)"
        try:
            resp = await self.ctx.brain.client.messages.create(
                model=self.ctx.cfg["llm"]["model"], max_tokens=200,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "Em uma frase curta, em português, o que mudou "
                     "ou o que há de relevante nesta tela?"}] + blocks[1:]}])
            return "".join(b.text for b in resp.content
                           if getattr(b, "type", None) == "text").strip() or "(sem descrição)"
        except Exception as e:  # noqa: BLE001
            return f"(não consegui descrever: {e})"

    # -------- loop de visão contínua --------
    async def _watch_loop(self, interval: float, sensitivity: int, mode: str) -> None:
        self._prev_hash = None
        last_desc = 0.0
        last_notify = 0.0
        loop = asyncio.get_running_loop()
        while not self._watch_stop.is_set():
            try:
                png = await loop.run_in_executor(None, lambda: _capture(timeout=6))
                if png:
                    h = _ahash(png)
                    if (h is not None and self._prev_hash is not None
                            and _hamming(h, self._prev_hash) >= sensitivity):
                        now = time.monotonic()
                        self.ctx.bus.publish("vision.change", {"mode": mode})
                        if now - last_notify > 8:        # throttle: sem tempestade de avisos
                            await loop.run_in_executor(None, lambda: _mac.h_notify(
                                {"text": "Mudança detectada na tela.",
                                 "title": "Ultravision"}, None))
                            last_notify = now
                        if mode == "describe" and now - last_desc > 15:
                            desc = await self._describe(png)
                            self.ctx.bus.publish("speech", {"role": "jarvis",
                                                            "text": f"Vi na tela: {desc}"})
                            if self.speak:
                                await self.speak(f"Vi uma mudança na tela. {desc}")
                            last_desc = now
                    if h is not None:
                        self._prev_hash = h
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  (nunca deixa o loop de visão morrer calado)
                pass
            try:
                await asyncio.wait_for(self._watch_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def watch_start(self, interval: float, sensitivity: int, mode: str) -> str:
        if self._watch_task and not self._watch_task.done():
            return "[ok] a visão contínua já está ativa"
        self._watch_stop = asyncio.Event()
        self._watch_task = asyncio.create_task(
            self._watch_loop(max(1.0, interval), max(1, min(64, sensitivity)), mode))
        return (f"[ok] visão contínua ativada (a cada {interval:.0f}s, modo {mode}). "
                f"Aviso quando a tela mudar.")

    def watch_stop(self) -> str:
        if not self._watch_task or self._watch_task.done():
            return "[ok] a visão contínua não estava ativa"
        self._watch_stop.set()
        self._watch_task.cancel()
        self._watch_task = None
        return "[ok] visão contínua desativada"

    def shutdown(self) -> None:
        self._watch_stop.set()
        if self._watch_task:
            self._watch_task.cancel()

    # -------- tools --------
    def tool_specs(self) -> list[ToolSpec]:
        def look(args, ctx):
            png = self._resolve_and_capture(args)
            if png is None:
                return "[erro] não consegui capturar a tela (sem permissão de Gravação de Tela?)."
            alvo = ("a janela em foco" if args.get("window")
                    else f"o monitor {args['display']}" if args.get("display")
                    else "a tela")
            return _image_blocks(png, f"Captura de {alvo}. Analise conforme pedido.")

        def read_text(args, ctx):
            png = self._resolve_and_capture(args)
            if png is None:
                return "[erro] não consegui capturar a tela."
            text = _ocr(png)
            if text is None:   # sem OCR local: devolve a imagem pro modelo transcrever
                blocks = _image_blocks(png, "OCR local indisponível — transcreva o texto "
                                            "visível nesta imagem.")
                return blocks
            return text[:8000] + ("\n[...truncado...]" if len(text) > 8000 else "")

        def displays(args, ctx):
            n = _count_displays()
            return f"{n} monitor(es) detectado(s). Use display: 1..{n} em vision_look."

        async def watch_start_tool(a, c):   # async -> roda no event loop (create_task válido)
            return self.watch_start(float(a.get("interval", 4) or 4),
                                    int(a.get("sensitivity", 8) or 8),
                                    a.get("mode", "notify"))

        async def watch_stop_tool(a, c):
            return self.watch_stop()

        return [
            ToolSpec("vision_look",
                     "Olha a tela e te envia a imagem para análise. Opcional: display (número "
                     "do monitor), window=true (só a janela em foco), ou região x/y/w/h em pixels.",
                     {"type": "object", "properties": {
                         "display": {"type": "integer"},
                         "window": {"type": "boolean"},
                         "x": {"type": "integer"}, "y": {"type": "integer"},
                         "w": {"type": "integer"}, "h": {"type": "integer"}},
                      "required": []},
                     "read", look),
            ToolSpec("vision_read_text",
                     "Lê (OCR) o texto que está na tela — ótimo para copiar erros, códigos, "
                     "textos de apps. Aceita os mesmos alvos do vision_look.",
                     {"type": "object", "properties": {
                         "display": {"type": "integer"},
                         "window": {"type": "boolean"},
                         "x": {"type": "integer"}, "y": {"type": "integer"},
                         "w": {"type": "integer"}, "h": {"type": "integer"}},
                      "required": []},
                     "read", read_text),
            ToolSpec("vision_displays", "Lista quantos monitores existem.",
                     {"type": "object", "properties": {}, "required": []},
                     "read", displays),
            ToolSpec("vision_watch_start",
                     "Liga a VISÃO CONTÍNUA: observa a tela em segundo plano e avisa quando "
                     "algo muda. mode 'notify' (só avisa) ou 'describe' (descreve e fala). "
                     "interval em segundos, sensitivity 1-64 (menor = mais sensível).",
                     {"type": "object", "properties": {
                         "interval": {"type": "number", "description": "padrão 4"},
                         "sensitivity": {"type": "integer", "description": "padrão 8"},
                         "mode": {"type": "string", "enum": ["notify", "describe"]}},
                      "required": []},
                     "low", watch_start_tool),
            ToolSpec("vision_watch_stop", "Desliga a visão contínua.",
                     {"type": "object", "properties": {}, "required": []},
                     "low", watch_stop_tool),
        ]
