import asyncio
from types import SimpleNamespace

from voice_agent.api import AudioInput


async def test_capture_keeps_new_audio_while_transcription_is_busy():
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    recorded = []
    turns = []

    class Audio:
        async def transcribe(self, pcm):
            recorded.append(pcm)
            if len(recorded) == 1:
                first_started.set()
                await release_first.wait()
            return "transcript"

    class Controller:
        audio = Audio()

        def emit(self, *args):
            pass

        async def turn(self, sid, owner, text, voice):
            turns.append(text)

    collector = AudioInput(Controller(), SimpleNamespace(row={"id": "session"}), "owner")
    collector.enabled = True
    await collector.frame(bytes([1, 0]) * 320)
    collector.finish()
    await first_started.wait()
    await collector.frame(bytes([2, 0]) * 320)
    collector.finish()
    release_first.set()
    await asyncio.wait_for(collector.transcription, 2)
    assert recorded == [bytes([1, 0]) * 320, bytes([2, 0]) * 320]
    assert len(turns) == 2
    await collector.close()
