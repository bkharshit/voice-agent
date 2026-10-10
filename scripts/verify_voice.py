"""Optional real speech smoke test against a running local backend.

Sends synthetic speech through the public PCM protocol. Requires local audio models.
This makes two short inference requests using the server's configured default profile.
"""

import asyncio
import base64
import io
import json

import httpx
import numpy as np
import soundfile as sf
import websockets
from scipy.signal import resample_poly
from voice_agent.settings import Settings
from voice_agent.speech import LocalAudio


async def main():
    settings = Settings()
    audio = LocalAudio(settings)
    await audio.warmup()
    wav = await audio.synthesize("Hello. This is a test of the voice agent.")
    samples, rate = sf.read(io.BytesIO(wav), dtype="float32")
    pcm = (np.clip(resample_poly(samples, 16000, rate), -1, 1) * 32767).astype("<i2").tobytes()
    # Silence provides a hands-free end-of-turn signal.
    pcm += bytes(32000)
    pcm += bytes((-len(pcm)) % 640)
    headers = {"Authorization": "Bearer " + settings.api_key} if settings.api_key else {}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", headers=headers) as client:
        session_response = await client.post("/v1/sessions", json={})
        session_response.raise_for_status()
        sid = session_response.json()["id"]
        try:
            connection = (await client.post(f"/v1/sessions/{sid}/connection", json={})).json()
            url = f"ws://127.0.0.1:8000{connection['path']}?ticket={connection['ticket']}"
            async with websockets.connect(url, origin="http://localhost:8000", max_size=5_000_000) as ws:
                await ws.recv()  # Connected event.
                for hands_free in (False, True):
                    await ws.send(json.dumps({"type": "capture_start", "hands_free": hands_free}))
                    for offset in range(0, len(pcm), 640):
                        await ws.send(pcm[offset : offset + 640])
                        await asyncio.sleep(0.004)
                    if not hands_free:
                        await ws.send(json.dumps({"type": "capture_stop"}))
                    generation = None
                    end = 0
                    received_audio = False
                    async with asyncio.timeout(90):
                        async for packet in ws:
                            event = json.loads(packet)
                            if event["type"] == "error":
                                raise RuntimeError(event["data"]["message"])
                            if event["type"] == "turn_committed":
                                generation = event["generation_id"]
                                print("Transcribed:", event["data"]["text"])
                            elif event["type"] == "speech_segment":
                                data = event["data"]
                                output, output_rate = sf.read(io.BytesIO(base64.b64decode(data["audio"])))
                                assert len(output) > 100 and output_rate == 24000
                                received_audio = True
                                end = data["end_char"]
                                await ws.send(
                                    json.dumps(
                                        {
                                            "type": "playback",
                                            "data": {"generation_id": generation, "chars": end},
                                        }
                                    )
                                )
                            elif event["type"] == "response_completed":
                                assert event["data"]["status"] == "complete" and received_audio
                                await ws.send(
                                    json.dumps(
                                        {
                                            "type": "playback",
                                            "data": {
                                                "generation_id": generation,
                                                "chars": end,
                                                "complete": True,
                                            },
                                        }
                                    )
                                )
                            elif event["type"] == "playback_completed":
                                print("Passed:", "hands-free" if hands_free else "push-to-talk")
                                break
                    if hands_free:
                        await ws.send(json.dumps({"type": "capture_cancel"}))
        finally:
            await client.delete(f"/v1/sessions/{sid}")


if __name__ == "__main__":
    asyncio.run(main())
