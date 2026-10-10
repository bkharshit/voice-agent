// Capture only. No microphone audio is routed to the speakers.
class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.sum = 0;
    this.count = 0;
    this.phase = 0;
    this.capturing = true;
    this.port.onmessage = (event) => {
      if (event.data?.type === "flush") {
        this.capturing = false;
        if (this.samples.length) {
          while (this.samples.length < 320) this.samples.push(0);
          this.emitFrame();
        }
        this.port.postMessage({ type: "flushed" });
      }
    };
  }
  emitFrame() {
    const buffer = new ArrayBuffer(640),
      view = new DataView(buffer);
    this.samples.forEach((value, i) => view.setInt16(i * 2, value, true));
    this.port.postMessage(buffer, [buffer]);
    this.samples = [];
  }
  process(inputs) {
    if (!this.capturing) return true;
    const input = inputs[0]?.[0];
    if (!input) return true;
    const ratio = sampleRate / 16000;
    for (const sample of input) {
      this.sum += sample;
      this.count++;
      this.phase++;
      if (this.phase >= ratio) {
        this.phase -= ratio;
        this.samples.push(
          Math.max(
            -32768,
            Math.min(32767, Math.round((this.sum / this.count) * 32767)),
          ),
        );
        this.sum = 0;
        this.count = 0;
        if (this.samples.length === 320) {
          this.emitFrame();
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm-capture", PCMProcessor);
