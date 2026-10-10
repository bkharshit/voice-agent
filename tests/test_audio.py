import pytest
from voice_agent.settings import Settings
from voice_agent.speech import LocalAudio


async def test_pipecat_detector_initializes_frame_parameters():
    pytest.importorskip("pipecat")
    from pipecat.audio.vad.vad_analyzer import VADState

    audio = LocalAudio(Settings(_env_file=None, mode="demo", audio_backend="local"))
    detector = audio.detector()
    try:
        assert await detector.analyze_audio(bytes(640)) == VADState.QUIET
        assert await detector.analyze_audio(bytes(640)) == VADState.QUIET
    finally:
        await detector.cleanup()


async def test_transcription_uses_configured_language_beam_and_padding():
    import numpy as np

    calls = []

    class Model:
        def transcribe(self, samples, **kwargs):
            calls.append(kwargs)
            return [], None

    audio = LocalAudio(Settings(_env_file=None, mode="demo", audio_backend="local"))
    audio.whisper = Model()
    assert await audio.transcribe(np.zeros(320, dtype="<i2").tobytes()) == ""
    assert calls[0]["language"] == "en" and calls[0]["beam_size"] == 5
    assert calls[0]["vad_parameters"]["speech_pad_ms"] == 400


async def test_vosk_collects_decoder_segments_and_final_result(monkeypatch):
    import sys
    from types import SimpleNamespace

    from voice_agent.speech import VoskRecognizer

    decoders = []

    class Decoder:
        def __init__(self, model, rate):
            assert rate == 16000
            decoders.append(self)

        def AcceptWaveform(self, data):
            return True

        def Result(self):
            return '{"text": "hello"}'

        def FinalResult(self):
            return '{"text": "world"}'

    monkeypatch.setitem(sys.modules, "vosk", SimpleNamespace(KaldiRecognizer=Decoder))
    recognizer = VoskRecognizer(Settings(_env_file=None, mode="demo", stt_provider="vosk"))
    assert recognizer.transcribe(bytes(640)) == "hello world"
    assert recognizer.transcribe(bytes(640)) == "hello world"
    assert len(decoders) == 2


def test_independent_speech_provider_defaults():
    settings = Settings(_env_file=None, mode="demo", audio_backend="local", stt_provider="browser")
    assert settings.stt_provider == "browser"
    assert settings.tts_provider == "kokoro"
