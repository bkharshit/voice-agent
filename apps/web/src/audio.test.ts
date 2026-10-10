import { describe, expect, it, vi } from "vitest";
import { Speaker } from "./audio";

describe("playback epochs", () => {
  it("does not acknowledge cancelled speech, even when an old onend arrives", async () => {
    const utterances: { onend: () => void }[] = [];
    vi.stubGlobal("window", {
      speechSynthesis: {
        speak: vi.fn((u) => utterances.push(u)),
        cancel: vi.fn(),
      },
    });
    vi.stubGlobal(
      "SpeechSynthesisUtterance",
      class {
        onend = () => {};
        constructor(public text: string) {}
      },
    );
    const receipt = vi.fn();
    const speaker = new Speaker(receipt, vi.fn(), vi.fn());
    speaker.enqueue({ generation: "old", text: "Old response", end: 12 });
    speaker.stop();
    utterances[0].onend();
    await Promise.resolve();
    expect(receipt).not.toHaveBeenCalled();
  });
  it("acknowledges only finished segments and sends completion after generation ends", async () => {
    const utterances: { onend: () => void }[] = [];
    vi.stubGlobal("window", {
      speechSynthesis: {
        speak: vi.fn((u) => utterances.push(u)),
        cancel: vi.fn(),
      },
    });
    vi.stubGlobal(
      "SpeechSynthesisUtterance",
      class {
        onend = () => {};
        constructor(public text: string) {}
      },
    );
    const receipt = vi.fn();
    const speaker = new Speaker(receipt, vi.fn(), vi.fn());
    speaker.enqueue({ generation: "new", text: "New response", end: 12 });
    expect(receipt).not.toHaveBeenCalled();
    utterances[0].onend();
    await Promise.resolve();
    await Promise.resolve();
    expect(receipt).toHaveBeenLastCalledWith(
      expect.objectContaining({ generation: "new" }),
      false,
    );
    speaker.complete("new");
    expect(receipt).toHaveBeenLastCalledWith(
      expect.objectContaining({ generation: "new" }),
      true,
    );
  });
});
