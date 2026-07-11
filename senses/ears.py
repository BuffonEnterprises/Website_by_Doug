"""Escuta (STT) + wake word.

Camadas (cada uma degrada para a próxima se faltar dependência):
  wake word  : openWakeWord ("hey jarvis")  ->  senão push-to-talk (Enter)
  captura    : sounddevice + VAD por energia  ->  senão SpeechRecognition
  transcrição: faster-whisper  ->  senão whisper (openai)  ->  senão modo texto

Tudo que é bloqueante roda em executor para não travar o event loop.
"""
from __future__ import annotations

import asyncio
import threading


class Ears:
    def __init__(self, cfg: dict):
        v = cfg.get("voice", {})
        self.mode = v.get("input", "auto")          # auto | wake | ptt | text
        self.language = v.get("stt", {}).get("language", "pt")
        self.stt_engine = v.get("stt", {}).get("engine", "auto")
        self.stt_model = v.get("stt", {}).get("model", "small")
        self.wake_model = v.get("wake", {}).get("model", "hey_jarvis")
        self.wake_threshold = float(v.get("wake", {}).get("threshold", 0.55))
        self._sr = None
        self._recognizer = None
        self._mic = None
        self._fw = None            # faster-whisper model
        self._oww = None           # openWakeWord model
        self._wake_stop = threading.Event()
        self._resolve_mode()

    # ------------------------------------------------------------------ #
    def _resolve_mode(self) -> None:
        if self.mode == "text":
            return
        # transcrição
        if not self._init_stt():
            print("[voz] STT indisponível — caindo para modo texto.")
            self.mode = "text"
            return
        # wake word
        if self.mode in ("auto", "wake"):
            if self._init_wake():
                self.mode = "wake"
            else:
                if self.mode == "wake":
                    print("[voz] wake word indisponível — usando push-to-talk.")
                self.mode = "ptt"

    def _init_stt(self) -> bool:
        try:
            import speech_recognition as sr
        except ImportError:
            return False
        try:
            self._sr = sr
            self._recognizer = sr.Recognizer()
            self._recognizer.pause_threshold = 0.9
            self._mic = sr.Microphone()
            with self._mic as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.6)
        except Exception as e:  # noqa: BLE001
            print(f"[voz] microfone indisponível ({e}).")
            return False
        # tenta faster-whisper (melhor); senão usa recognize_whisper depois
        if self.stt_engine in ("auto", "faster-whisper"):
            try:
                from faster_whisper import WhisperModel
                self._fw = WhisperModel(self.stt_model, device="cpu", compute_type="int8")
            except Exception:  # noqa: BLE001
                self._fw = None
        return True

    def _init_wake(self) -> bool:
        try:
            import numpy as np  # noqa: F401
            import sounddevice as sd  # noqa: F401  (necessário no _wake_blocking)
            from openwakeword.model import Model
            self._oww = Model(wakeword_models=[self.wake_model])
            return True
        except Exception:  # noqa: BLE001
            return False

    def shutdown(self) -> None:
        """Sinaliza o loop de wake word a parar (libera o microfone no encerramento)."""
        self._wake_stop.set()

    # ------------------------------------------------------------------ #
    async def wait_for_trigger(self) -> None:
        """Bloqueia até o gatilho: wake word, ou Enter (ptt), ou nada (text)."""
        if self.mode == "text":
            return
        if self.mode == "ptt":
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: input("\n[Enter para falar] "))
            return
        await asyncio.get_running_loop().run_in_executor(None, self._wake_blocking)

    def _wake_blocking(self) -> None:
        import numpy as np
        import sounddevice as sd
        self._oww.reset()
        self._wake_stop.clear()
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                            blocksize=1280) as stream:
            while not self._wake_stop.is_set():  # checável a cada 80ms -> cancelável
                frame, _ = stream.read(1280)
                audio = np.frombuffer(frame, dtype=np.int16)
                scores = self._oww.predict(audio)
                if any(s >= self.wake_threshold for s in scores.values()):
                    return

    # ------------------------------------------------------------------ #
    async def listen(self) -> str:
        if self.mode == "text":
            try:
                return await asyncio.get_running_loop().run_in_executor(
                    None, lambda: input("você> ").strip())
            except EOFError:
                return "sair"
        return await asyncio.get_running_loop().run_in_executor(None, self._listen_blocking)

    def _listen_blocking(self) -> str:
        sr = self._sr
        print("(ouvindo…)")
        try:
            with self._mic as source:
                audio = self._recognizer.listen(source, phrase_time_limit=25)
        except Exception as e:  # noqa: BLE001
            print(f"[voz] erro ao capturar: {e}")
            return ""
        # faster-whisper
        if self._fw is not None:
            try:
                import io
                import wave
                import numpy as np
                # faster-whisper espera 16 kHz mono; força a conversão antes de ler os frames
                raw = audio.get_wav_data(convert_rate=16000, convert_width=2)
                with wave.open(io.BytesIO(raw), "rb") as wf:
                    frames = wf.readframes(wf.getnframes())
                samples = (np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0)
                segments, _ = self._fw.transcribe(samples, language=self.language, beam_size=1)
                return "".join(seg.text for seg in segments).strip()
            except Exception as e:  # noqa: BLE001
                print(f"[voz] faster-whisper falhou ({e}); tentando whisper padrão.")
        # fallback: whisper via SpeechRecognition
        try:
            return self._recognizer.recognize_whisper(
                audio, model=self.stt_model, language=self.language).strip()
        except sr.UnknownValueError:
            return ""
        except Exception as e:  # noqa: BLE001
            print(f"[voz] não entendi ({e}).")
            return ""
