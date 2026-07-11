"""Fala (TTS) com fila assíncrona e streaming frase-a-frase.

say() apenas ENFILEIRA e retorna na hora — assim o cérebro continua transmitindo
sem travar. Um worker reproduz as frases em ordem. stop() esvazia a fila e mata a
reprodução atual (barge-in / "para tudo"). Motores: say (macOS), ElevenLabs,
OpenAI; degrada para impressão em tela se nenhum estiver disponível.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile


class Mouth:
    def __init__(self, cfg: dict, bus=None):
        tts = cfg.get("voice", {}).get("tts", {})
        self.engine = tts.get("engine", "say")
        self.voice = tts.get("voice", "Luciana")
        self.rate = int(tts.get("rate", 190))
        self.el_voice = tts.get("elevenlabs_voice_id", "")
        self.el_model = tts.get("elevenlabs_model", "eleven_multilingual_v2")
        self.oa_voice = tts.get("openai_voice", "alloy")
        self.bus = bus
        self._queue: asyncio.Queue = asyncio.Queue()
        self._proc: subprocess.Popen | None = None
        self._worker: asyncio.Task | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    def start(self) -> None:
        if not self._worker:
            self._worker = asyncio.create_task(self._run())

    async def say(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        print(f"jarvis> {text}")
        if self.bus:
            self.bus.publish("speech", {"role": "jarvis", "text": text})
        if self.engine == "none":
            return
        self._idle.clear()
        await self._queue.put(text)

    async def drain(self) -> None:
        """Espera a fila esvaziar e a fala atual terminar (antes de ouvir de novo)."""
        await self._queue.join()
        await self._idle.wait()

    async def stop(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._idle.set()

    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._speak_blocking, text)
            except Exception as e:  # noqa: BLE001
                print(f"[voz] falha no TTS: {e}")
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._idle.set()

    def _speak_blocking(self, text: str) -> None:
        if self.engine == "elevenlabs" and self._eleven(text):
            return
        if self.engine == "openai" and self._openai(text):
            return
        self._say_native(text)

    def _say_native(self, text: str) -> None:
        try:
            self._proc = subprocess.Popen(["say", "-v", self.voice, "-r", str(self.rate), text])
            self._proc.wait()
            if self._proc.returncode not in (0, None):
                subprocess.run(["say", "-r", str(self.rate), text])
        except FileNotFoundError:
            pass  # não é macOS: já imprimimos o texto

    def _play_bytes(self, data: bytes, suffix: str) -> None:
        path = os.path.join(tempfile.gettempdir(), f"jarvis_tts{suffix}")
        with open(path, "wb") as f:
            f.write(data)
        try:
            self._proc = subprocess.Popen(["afplay", path])
            self._proc.wait()
        except FileNotFoundError:
            self._say_native("")  # sem player: ignora

    def _eleven(self, text: str) -> bool:
        key = os.getenv("ELEVENLABS_API_KEY")
        if not key or not self.el_voice:
            return False
        try:
            import requests
            r = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{self.el_voice}",
                headers={"xi-api-key": key, "accept": "audio/mpeg"},
                json={"text": text, "model_id": self.el_model}, timeout=30,
            )
            r.raise_for_status()
            self._play_bytes(r.content, ".mp3")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[voz] ElevenLabs falhou ({e}); usando voz nativa.")
            return False

    def _openai(self, text: str) -> bool:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            return False
        try:
            import requests
            r = requests.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": "tts-1", "voice": self.oa_voice, "input": text}, timeout=30,
            )
            r.raise_for_status()
            self._play_bytes(r.content, ".mp3")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[voz] OpenAI TTS falhou ({e}); usando voz nativa.")
            return False
