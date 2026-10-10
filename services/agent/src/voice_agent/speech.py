"""Phrase segmentation and optional local inference. No paid audio services."""

import asyncio
import io
import json
import re
from typing import Protocol

from voice_agent.domain import AppError
from voice_agent.settings import Settings


class PhraseBuffer:
    def __init__(self):
        self.pending = ""

    def add(self, text: str, flush: bool = False) -> list[str]:
        self.pending += text
        phrases = []
        while self.pending:
            # Prefer sentence punctuation over arbitrary token or comma boundaries.
            boundary = re.search(r"[!?](?:\s|$)|(?<!\d)\.(?=\s|$)|\n", self.pending)
            if boundary and boundary.end() >= 20:
                end = boundary.end()
            elif len(self.pending) >= 240:
                end = self.pending.rfind(" ", 80, 220)
                if end < 0:
                    end = 220
            elif flush:
                end = len(self.pending)
            else:
                break
            phrase = self.pending[:end]
            self.pending = self.pending[end:]
            if phrase:
                phrases.append(phrase)
        return phrases


class LocalAudio:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stt_lock = asyncio.Lock()
        self.tts_lock = asyncio.Lock()
        self.whisper = None
        self.kokoro = None

    async def warmup(self):
        factory = STT_PROVIDERS.get(self.settings.stt_provider)
        if self.settings.stt_provider != "browser" and factory is None:
            raise AppError(f"Unknown speech provider: {self.settings.stt_provider}", 503)
        self.recognizer = factory(self.settings) if factory else None

        def load():
            if self.recognizer:
                self.recognizer.load()
            if self.settings.tts_provider == "kokoro":
                if not self.settings.kokoro_model.is_file() or not self.settings.kokoro_voices.is_file():
                    raise AppError("Download Kokoro assets: voice-agent download-models", 503)
                from kokoro_onnx import Kokoro

                self.kokoro = Kokoro(str(self.settings.kokoro_model), str(self.settings.kokoro_voices))

        try:
            await asyncio.to_thread(load)
        except ImportError as exc:
            raise AppError("Install speech dependencies: uv sync --extra voice --extra vosk", 503) from exc

    def detector(self):
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams

        detector = SileroVADAnalyzer(
            sample_rate=16000,
            params=VADParams(start_secs=0.18, stop_secs=self.settings.vad_stop_seconds, min_volume=0.5),
        )
        detector.set_sample_rate(16000)
        return detector

    async def transcribe(self, pcm: bytes) -> str:
        async with self.stt_lock:
            # Preserve direct model injection for tests and existing integrations.
            recognizer = getattr(self, "recognizer", None)
            if recognizer is None and self.whisper is not None:
                recognizer = WhisperRecognizer(self.settings)
                recognizer.model = self.whisper
            if recognizer is None:
                raise AppError("Server recognition is not enabled", 422)
            return await self._cpu(lambda: recognizer.transcribe(pcm))

    async def synthesize(self, text: str) -> bytes:
        import soundfile as sf

        async with self.tts_lock:

            def run():
                samples, rate = self.kokoro.create(
                    text, voice=self.settings.kokoro_voice, speed=1.0, lang="en-us"
                )
                output = io.BytesIO()
                sf.write(output, samples, rate, format="WAV", subtype="PCM_16")
                return output.getvalue()

            return await self._cpu(run)

    @staticmethod
    async def _cpu(function):
        task = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Threads cannot be cancelled. Keep the model lock until inference really finishes.
            await asyncio.gather(task, return_exceptions=True)
            raise


class SpeechRecognizer(Protocol):
    """Adapters consume PCM16 mono at 16kHz; models load once per server."""

    def load(self) -> None: ...
    def transcribe(self, pcm: bytes) -> str: ...


class WhisperRecognizer:
    def __init__(self, settings):
        self.settings = settings
        self.model = None

    def load(self):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(
            self.settings.whisper_model,
            device="cpu",
            compute_type="int8",
            download_root=".data/models/whisper",
        )

    def transcribe(self, pcm):
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            samples,
            language=None if self.settings.whisper_language == "auto" else self.settings.whisper_language,
            beam_size=self.settings.whisper_beam_size,
            vad_filter=True,
            vad_parameters={"speech_pad_ms": 400, "min_silence_duration_ms": 700},
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


class VoskRecognizer:
    def __init__(self, settings):
        self.settings = settings
        self.model = None

    def load(self):
        from vosk import Model

        if not self.settings.vosk_model.is_dir():
            raise AppError("Download a Vosk model and set VOICE_VOSK_MODEL; see README", 503)
        self.model = Model(str(self.settings.vosk_model))

    def transcribe(self, pcm):
        from vosk import KaldiRecognizer

        # Decoder state belongs to this utterance, never to the shared model.
        decoder = KaldiRecognizer(self.model, 16000)
        results = []
        for start in range(0, len(pcm), 8000):
            if decoder.AcceptWaveform(pcm[start : start + 8000]):
                results.append(json.loads(decoder.Result()).get("text", ""))
        results.append(json.loads(decoder.FinalResult()).get("text", ""))
        return " ".join(text for text in results if text).strip()


# Register additional adapters here without changing transport or agent orchestration.
STT_PROVIDERS: dict[str, type[SpeechRecognizer]] = {
    "whisper": WhisperRecognizer,
    "vosk": VoskRecognizer,
}
