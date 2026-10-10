export type Segment = {
  generation: string;
  text: string;
  end: number;
  audio?: string;
};

export class Speaker {
  private epoch = 0;
  private queue: Segment[] = [];
  private playing = false;
  private audio: HTMLAudioElement | null = null;
  private cancelPlayback: (() => void) | null = null;
  private finished = new Set<string>();
  private last: Segment | null = null;
  constructor(
    private onDelivery: (segment: Segment, complete: boolean) => void,
    private onState: (speaking: boolean) => void,
    private onError: (message: string) => void,
  ) {}

  enqueue(segment: Segment) {
    this.queue.push(segment);
    void this.drain();
  }
  complete(generation: string) {
    this.finished.add(generation);
    if (
      !this.playing &&
      !this.queue.length &&
      this.last?.generation === generation
    )
      this.onDelivery(this.last, true);
  }
  stop() {
    this.epoch++;
    this.queue = [];
    this.finished.clear();
    this.last = null;
    this.cancelPlayback?.();
    this.cancelPlayback = null;
    window.speechSynthesis?.cancel();
    this.audio?.pause();
    this.audio = null;
    this.playing = false;
    this.onState(false);
  }
  private async drain() {
    if (this.playing) return;
    const epoch = this.epoch;
    this.playing = true;
    while (this.queue.length && epoch === this.epoch) {
      const segment = this.queue.shift()!;
      this.onState(true);
      let delivered = false;
      try {
        delivered = await new Promise<boolean>((resolve) => {
          this.cancelPlayback = () => resolve(false);
          if (segment.audio) {
            const bytes = Uint8Array.from(atob(segment.audio), (c) =>
              c.charCodeAt(0),
            );
            const url = URL.createObjectURL(
              new Blob([bytes], { type: "audio/wav" }),
            );
            const audio = new Audio(url);
            this.audio = audio;
            const finish = (ok: boolean) => {
              URL.revokeObjectURL(url);
              resolve(ok);
            };
            this.cancelPlayback = () => {
              audio.pause();
              finish(false);
            };
            audio.onended = () => finish(true);
            audio.onerror = () => finish(false);
            void audio.play().catch(() => finish(false));
          } else if ("speechSynthesis" in window) {
            const utterance = new SpeechSynthesisUtterance(segment.text);
            utterance.rate = 1.04;
            utterance.onend = () => resolve(true);
            utterance.onerror = () => resolve(false);
            window.speechSynthesis.speak(utterance);
          } else resolve(false);
        });
      } catch {
        delivered = false;
      }
      if (epoch !== this.epoch) return;
      this.cancelPlayback = null;
      if (!delivered) {
        this.queue = [];
        this.onError(
          "Audio playback failed. Use text mode or enable audio in your browser.",
        );
        break;
      }
      this.last = segment;
      this.onDelivery(
        segment,
        this.finished.has(segment.generation) && !this.queue.length,
      );
    }
    if (epoch === this.epoch) {
      this.playing = false;
      this.onState(false);
    }
  }
}

export class Microphone {
  private epoch = 0;
  private context: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private flushed: (() => void) | null = null;
  async start(
    socket: WebSocket,
    handsFree: boolean,
    onError: (message: string) => void = () => {},
  ) {
    const epoch = ++this.epoch;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (epoch !== this.epoch) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      this.stream = stream;
      this.context = new AudioContext({ sampleRate: 16000 });
      await this.context.audioWorklet.addModule("/capture-worklet.js?v=2");
      if (epoch !== this.epoch) return;
      await this.context.resume();
      this.node = new AudioWorkletNode(this.context, "pcm-capture");
      this.node.port.onmessage = (event) => {
        if (event.data?.type === "flushed") {
          this.flushed?.();
          return;
        }
        if (
          socket.readyState !== WebSocket.OPEN ||
          socket.bufferedAmount > 256000
        ) {
          if (socket.readyState === WebSocket.OPEN)
            socket.send(JSON.stringify({ type: "capture_cancel" }));
          this.stop();
          onError(
            "Audio connection is too slow or disconnected. Please reconnect and repeat your turn.",
          );
          return;
        }
        socket.send(event.data);
      };
      socket.send(
        JSON.stringify({ type: "capture_start", hands_free: handsFree }),
      );
      this.context.createMediaStreamSource(this.stream).connect(this.node);
      this.node.connect(this.context.destination);
    } catch (error) {
      this.stop();
      throw error;
    }
  }
  async finish(socket: WebSocket) {
    const epoch = this.epoch;
    if (this.node) {
      await new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(() => {
          this.flushed = null;
          this.stop();
          reject(
            new Error(
              "Microphone did not finish capturing. Please repeat your turn.",
            ),
          );
        }, 2000);
        this.flushed = () => {
          clearTimeout(timeout);
          this.flushed = null;
          resolve();
        };
        this.node!.port.postMessage({ type: "flush" });
      });
    }
    if (epoch !== this.epoch) return;
    if (socket.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify({ type: "capture_stop" }));
    this.stop();
  }
  stop() {
    this.epoch++;
    this.flushed?.();
    this.node?.disconnect();
    this.node = null;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    void this.context?.close().catch(() => {});
    this.context = null;
  }
}

export type Recognition = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult:
    | ((event: {
        results: { isFinal: boolean; [key: number]: { transcript: string } }[];
        resultIndex: number;
      }) => void)
    | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
};
export function recognitionConstructor(): (new () => Recognition) | undefined {
  return (
    (
      window as unknown as {
        SpeechRecognition?: new () => Recognition;
        webkitSpeechRecognition?: new () => Recognition;
      }
    ).SpeechRecognition ||
    (window as unknown as { webkitSpeechRecognition?: new () => Recognition })
      .webkitSpeechRecognition
  );
}
