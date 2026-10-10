import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, it, vi } from "vitest";

it("flushes the last partial audio frame before acknowledging capture completion", () => {
  let Processor: any;
  class Worklet {
    port = { onmessage: (_: any) => {}, postMessage: vi.fn() };
  }
  runInNewContext(
    readFileSync(
      new URL("../public/capture-worklet.js", import.meta.url),
      "utf8",
    ),
    {
      AudioWorkletProcessor: Worklet,
      sampleRate: 16000,
      registerProcessor: (_name: string, implementation: any) => {
        Processor = implementation;
      },
    },
  );
  const processor = new Processor();
  processor.process([[new Float32Array(500).fill(0.5)]]);
  expect(processor.port.postMessage.mock.calls.length).toBe(1);
  processor.port.onmessage({ data: { type: "flush" } });
  const calls = processor.port.postMessage.mock.calls;
  expect(calls.length).toBe(3);
  expect(calls[1][0].byteLength).toBe(640);
  const last = new DataView(calls[1][0]);
  expect(last.getInt16(0, true)).toBeGreaterThan(0);
  expect(last.getInt16(360, true)).toBe(0);
  expect(calls[2][0]).toEqual({ type: "flushed" });
  processor.process([[new Float32Array(320)]]);
  expect(processor.port.postMessage.mock.calls.length).toBe(3);
});
