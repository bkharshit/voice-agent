import { expect, it, vi } from "vitest";
import { Microphone } from "./audio";

it("releases a microphone granted after capture was cancelled", async () => {
  let grant!: (stream: MediaStream) => void;
  const permission = new Promise<MediaStream>((resolve) => {
    grant = resolve;
  });
  vi.stubGlobal("navigator", {
    mediaDevices: { getUserMedia: vi.fn(() => permission) },
  });
  const stop = vi.fn();
  const microphone = new Microphone();
  const pending = microphone.start({} as WebSocket, false);
  microphone.stop();
  grant({ getTracks: () => [{ stop }] } as unknown as MediaStream);
  await pending;
  expect(stop).toHaveBeenCalledOnce();
});
