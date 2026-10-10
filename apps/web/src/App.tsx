import { useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowUp,
  Check,
  ChevronDown,
  Headphones,
  KeyRound,
  Mic,
  Plus,
  Radio,
  Settings2,
  Square,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import {
  VoiceClient,
  type AgentEvent,
  type Capabilities,
  type Message,
  type Session,
} from "@voice-agent/client";
import {
  Microphone,
  recognitionConstructor,
  Speaker,
  type Recognition,
} from "./audio";

export default function App() {
  const [client, setClient] = useState(() => new VoiceClient());
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [status, setStatus] = useState("Connecting");
  const [error, setError] = useState("");
  const [spoken, setSpoken] = useState(true);
  const [recording, setRecording] = useState(false);
  const [handsFree, setHandsFree] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [needsKey, setNeedsKey] = useState(false);
  const [accessKey, setAccessKey] = useState("");
  const [switchImmediately, setSwitchImmediately] = useState(false);
  const [toolActivity, setToolActivity] = useState("");
  const socket = useRef<WebSocket | null>(null);
  const active = useRef<Session | null>(null);
  const generation = useRef<string | null>(null);
  const speaker = useRef<Speaker | null>(null);
  const microphone = useRef(new Microphone());
  const recognition = useRef<Recognition | null>(null);
  const end = useRef<HTMLDivElement | null>(null);
  const connected = useRef(false);
  const opening = useRef(0);
  const recordingRef = useRef(false);

  const fail = (e: unknown) =>
    setError(e instanceof Error ? e.message : String(e));

  async function stop(notify = true) {
    speaker.current?.stop();
    if (notify && active.current) {
      try {
        await client.interrupt(active.current.id, generation.current);
      } catch (e) {
        fail(e);
      }
    }
    generation.current = null;
    setStatus("Ready");
  }
  function stopMic(cancel = false) {
    recordingRef.current = false;
    setRecording(false);
    if (!cancel && caps?.stt_provider !== "browser" && socket.current) {
      void microphone.current.finish(socket.current).catch((e) => {
        socket.current?.send(JSON.stringify({ type: "capture_cancel" }));
        fail(e);
      });
      return;
    }
    microphone.current.stop();
    if (cancel) {
      const current = recognition.current;
      recognition.current = null;
      current?.abort();
    } else recognition.current?.stop();
    if (socket.current?.readyState === WebSocket.OPEN)
      socket.current.send(
        JSON.stringify({ type: cancel ? "capture_cancel" : "capture_stop" }),
      );
  }

  async function sendTurn(value: string, voice = spoken) {
    if (!active.current || !value.trim() || !connected.current) return;
    try {
      speaker.current?.stop();
      setError("");
      await client.turn(active.current.id, value, voice);
      setText("");
    } catch (e) {
      fail(e);
    }
  }

  async function onEvent(event: AgentEvent) {
    if (event.session_id !== active.current?.id) return;
    const data = event.data;
    if (event.type === "turn_committed") {
      speaker.current?.stop();
      generation.current = event.generation_id;
      setStatus("Thinking");
      setToolActivity("");
      setMessages((previous) => [
        ...previous,
        {
          id: `${event.turn_id}-user`,
          role: "user",
          text: String(data.text),
          generation_id: event.generation_id!,
          status: "complete",
          voice: false,
          delivered_chars: 0,
        },
        {
          id: `${event.turn_id}-assistant`,
          role: "assistant",
          text: "",
          generation_id: event.generation_id!,
          status: "generating",
          voice: Boolean(data.voice),
          delivered_chars: 0,
        },
      ]);
    } else if (
      event.type === "text_delta" &&
      generation.current === event.generation_id
    ) {
      setMessages((previous) =>
        previous.map((m) =>
          m.role === "assistant" && m.generation_id === event.generation_id
            ? { ...m, text: m.text + String(data.text) }
            : m,
        ),
      );
    } else if (
      event.type === "speech_segment" &&
      generation.current === event.generation_id
    ) {
      speaker.current?.enqueue({
        generation: event.generation_id!,
        text: String(data.text),
        end: Number(data.end_char),
        audio: typeof data.audio === "string" ? data.audio : undefined,
      });
    } else if (event.type === "interrupted") {
      speaker.current?.stop();
      generation.current = null;
      setStatus("Ready");
      setMessages((previous) =>
        previous.map((m) =>
          m.role === "assistant" && m.generation_id === event.generation_id
            ? { ...m, status: "interrupted" }
            : m,
        ),
      );
    } else if (event.type === "response_completed") {
      if (event.generation_id) speaker.current?.complete(event.generation_id);
      setMessages((previous) =>
        previous.map((m) =>
          m.role === "assistant" && m.generation_id === event.generation_id
            ? { ...m, status: String(data.status) }
            : m,
        ),
      );
      setStatus((previous) => (previous === "Speaking" ? previous : "Ready"));
    } else if (
      event.type === "configuration_applied" ||
      event.type === "configuration_pending"
    ) {
      const updated = { ...active.current, ...data } as Session;
      active.current = updated;
      setSession(updated);
    } else if (event.type === "tool_started") {
      setToolActivity(`Using ${String(data.name).replaceAll("_", " ")}`);
    } else if (
      event.type === "tool_completed" ||
      event.type === "tool_failed"
    ) {
      setToolActivity(
        event.type === "tool_completed" ? "Tool completed" : "Tool failed",
      );
    } else if (event.type === "error") {
      setError(String(data.message));
      setStatus("Ready");
    } else if (event.type === "transcribing") setStatus("Transcribing");
    else if (event.type === "speech_started") {
      speaker.current?.stop();
      setStatus("Starting microphone");
    } else if (
      event.type === "transcript_empty" ||
      event.type === "playback_completed"
    )
      setStatus("Ready");
    else if (event.type === "resync_required")
      setError("Connection fell behind. Reopen this session to synchronize.");
  }
  const eventHandler = useRef(onEvent);
  eventHandler.current = onEvent;

  async function openSession(next: Session) {
    const request = ++opening.current;
    stopMic(true);
    speaker.current?.stop();
    socket.current?.close();
    socket.current = null;
    connected.current = false;
    active.current = next;
    generation.current = null;
    setSession(next);
    setStatus("Connecting");
    setError("");
    setToolActivity("");
    try {
      const snapshot = await client.snapshot(next.id);
      if (request !== opening.current) return;
      active.current = snapshot;
      setSession(snapshot);
      setMessages(snapshot.messages || []);
      const ws = await client.connect(next.id);
      if (request !== opening.current) {
        ws.close();
        return;
      }
      socket.current = ws;
      connected.current = true;
      ws.onmessage = (e) => {
        void eventHandler.current(JSON.parse(e.data) as AgentEvent);
      };
      ws.onclose = (event) => {
        if (socket.current !== ws) return;
        connected.current = false;
        stopMic(true);
        speaker.current?.stop();
        setStatus("Disconnected");
        setError(
          event.code === 4001
            ? "This conversation is now open on another device or tab. Reconnect here to take over."
            : "Connection lost. Use Settings → Reconnect this conversation.",
        );
      };
      ws.onerror = () =>
        setError("Session connection failed. Reopen the session to reconnect.");
      setStatus("Ready");
      localStorage.setItem("voice-session", next.id);
    } catch (e) {
      fail(e);
      setStatus("Disconnected");
    }
  }

  async function newSession() {
    try {
      const next = await client.create(caps?.default_profile || "ollama-local");
      setSessions((previous) => [next, ...previous]);
      await openSession(next);
    } catch (e) {
      fail(e);
    }
  }

  useEffect(() => {
    let cancelled = false;
    speaker.current = new Speaker(
      (segment, complete) => {
        const current = active.current;
        if (!current || generation.current !== segment.generation) return;
        void client
          .receipt(current.id, segment.generation, segment.end, complete)
          .catch(fail);
      },
      (speaking) => setStatus(speaking ? "Speaking" : "Ready"),
      (message) => {
        setError(message);
        void stop();
      },
    );
    async function initialize() {
      try {
        const info = await client.capabilities();
        const list = await client.sessions();
        if (cancelled) return;
        setCaps(info);
        setSessions(list);
        setNeedsKey(false);
        const remembered = list.find(
          (s) => s.id === localStorage.getItem("voice-session"),
        );
        if (remembered || list[0]) await openSession(remembered || list[0]);
        else {
          const next = await client.create(info.default_profile);
          if (cancelled) return;
          setSessions([next]);
          await openSession(next);
        }
      } catch (e) {
        if (cancelled) return;
        fail(e);
        setStatus("Disconnected");
        if (e instanceof Error && e.message.includes("access key"))
          setNeedsKey(true);
      }
    }
    void initialize();
    return () => {
      cancelled = true;
      opening.current++;
      socket.current?.close();
      speaker.current?.stop();
      stopMic(true);
    };
  }, [client]);

  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function changeProfile(profile: string) {
    if (!active.current) return;
    try {
      if (switchImmediately) speaker.current?.stop();
      const updated = await client.configure(
        active.current,
        profile,
        switchImmediately,
      );
      active.current = updated;
      setSession(updated);
      setError("");
    } catch (e) {
      fail(e);
    }
  }

  async function startMic() {
    if (!active.current || !connected.current || recordingRef.current) return;
    try {
      setError("");
      await stop();
      recordingRef.current = true;
      setRecording(true);
      setStatus("Listening");
      if (caps?.stt_provider !== "browser") {
        if (!socket.current) throw new Error("Connect a session first");
        await microphone.current.start(socket.current, handsFree, (message) => {
          stopMic(true);
          setStatus("Ready");
          setError(message);
        });
        if (recordingRef.current) setStatus("Listening");
      } else {
        const Constructor = recognitionConstructor();
        if (!Constructor)
          throw new Error(
            "This browser does not support speech recognition. Use text or enable local audio.",
          );
        const rec = new Constructor();
        recognition.current = rec;
        rec.lang = "en-US";
        rec.interimResults = true;
        rec.continuous = false;
        let finalText = "";
        rec.onresult = (event) => {
          let interim = "";
          for (let i = event.resultIndex; i < event.results.length; i++) {
            const result = event.results[i];
            if (result.isFinal) finalText += result[0].transcript + " ";
            else interim += result[0].transcript;
          }
          setText(finalText + interim);
        };
        rec.onerror = (event) => {
          if (event.error !== "aborted") setError(`Microphone: ${event.error}`);
        };
        rec.onend = () => {
          if (recognition.current !== rec) return;
          recognition.current = null;
          recordingRef.current = false;
          setRecording(false);
          setStatus("Ready");
          if (finalText.trim()) void sendTurn(finalText, true);
        };
        rec.start();
        setStatus("Listening");
      }
    } catch (e) {
      stopMic(true);
      setStatus("Ready");
      fail(e);
    }
  }

  const currentProfile = caps?.profiles.find(
    (p) => p.id === session?.profile_id,
  );
  const busy = status === "Thinking" || status === "Speaking";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/">
          <span className="brand-icon">
            <Radio size={23} />
          </span>
          <span>
            Voice Agent<small>YOUR CONVERSATION SPACE</small>
          </span>
        </a>
        <button className="new-session" onClick={() => void newSession()}>
          <Plus size={17} /> New conversation
        </button>
        <div className="section-label">
          CONVERSATIONS <span>{sessions.length}</span>
        </div>
        <nav className="session-list">
          {sessions.map((s, i) => (
            <button
              key={s.id}
              className={session?.id === s.id ? "session active" : "session"}
              onClick={() => void openSession(s)}
            >
              <Headphones size={17} />
              <span>
                Conversation {sessions.length - i}
                <small>
                  {new Date(s.updated_at).toLocaleDateString(undefined, {
                    month: "short",
                    day: "numeric",
                  })}
                </small>
              </span>
              {session?.id === s.id && <span className="session-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className="dot" />
          <span>
            {caps?.mode === "demo" ? "Demo workspace" : "Local workspace"}
            <small>
              {caps?.database === "postgresql"
                ? "PostgreSQL connected"
                : caps
                  ? "SQLite demo storage"
                  : "Waiting for server"}
            </small>
          </span>
          <Settings2 size={17} />
        </div>
      </aside>
      <main>
        <header>
          <div>
            <span className="eyebrow">WORKSPACE / CONVERSATION</span>
            <h1>Let’s talk.</h1>
          </div>
          <div className="header-actions">
            <button
              className="mobile-new settings-button"
              aria-label="New conversation"
              onClick={() => void newSession()}
            >
              <Plus size={16} />
            </button>
            <button
              className="settings-button"
              onClick={() => setSettingsOpen(!settingsOpen)}
            >
              <Settings2 size={16} /> Settings
            </button>
          </div>
        </header>
        <section className="conversation">
          <div className="agent-card">
            <div className={`orb ${recording || busy ? "animated" : ""}`}>
              <Radio size={34} />
            </div>
            <div className="agent-heading">
              <span className="eyebrow">VOICE ASSISTANT</span>
              <h2>
                {recording
                  ? status === "Starting microphone"
                    ? "Getting ready…"
                    : "I’m listening."
                  : status === "Speaking"
                    ? "Thinking out loud."
                    : "A little more natural."}
              </h2>
              <p>Speak your mind, or start with a message.</p>
            </div>
            <span
              className={`status-pill ${status === "Disconnected" ? "offline" : ""}`}
            >
              <span className="dot" />
              {status}
            </span>
          </div>
          <div className="conversation-meta">
            <span>
              <Activity size={13} /> {currentProfile?.label || "Select a model"}
            </span>
            <span>
              {caps?.stt_provider !== "browser"
                ? "Self-hosted voice"
                : "Browser voice"}
            </span>
          </div>
          <div className="messages" aria-live="polite">
            {messages.length === 0 ? (
              <div className="empty-state">
                <span className="empty-label">START SOMETHING</span>
                <h3>Your next idea starts here.</h3>
                <p>
                  Ask a question. Work through a thought.
                  <br />
                  Keep the conversation going.
                </p>
                <div className="suggestions">
                  {[
                    "Help me plan my day",
                    "What can you help me with?",
                    "Let’s brainstorm an idea",
                  ].map((value) => (
                    <button key={value} onClick={() => void sendTurn(value)}>
                      {value}
                      <ArrowUp size={13} />
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((m) => (
                <article key={m.id} className={`message ${m.role}`}>
                  <span className="message-label">
                    {m.role === "user" ? "YOU" : "VOICE AGENT"}
                    {m.status === "interrupted" && <span> · interrupted</span>}
                  </span>
                  <p>
                    {m.text || (
                      <span className="thinking-dots">
                        Thinking<span>...</span>
                      </span>
                    )}
                  </p>
                </article>
              ))
            )}
            <div ref={end} />
          </div>
          {toolActivity && (
            <div className="tool-activity">
              <Check size={13} />
              {toolActivity}
            </div>
          )}
          {error && (
            <div className="error" role="alert">
              <span>{error}</span>
              <button aria-label="Dismiss error" onClick={() => setError("")}>
                <X size={15} />
              </button>
            </div>
          )}
          <div className="composer">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void sendTurn(text);
              }}
            >
              <textarea
                aria-label="Message"
                placeholder="Type a message, or use your voice…"
                value={text}
                rows={1}
                maxLength={8000}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void sendTurn(text);
                  }
                }}
              />
              <button
                className="send-button"
                type="submit"
                aria-label="Send message"
                disabled={!text.trim() || !connected.current}
              >
                <ArrowUp size={20} />
              </button>
            </form>
            <div className="composer-controls">
              <button
                className={recording ? "mic-button recording" : "mic-button"}
                onClick={() => (recording ? stopMic() : void startMic())}
                disabled={!session || status === "Disconnected"}
              >
                <Mic size={16} />
                {recording
                  ? status === "Starting microphone"
                    ? "Starting microphone…"
                    : "Finish speaking"
                  : "Start speaking"}
              </button>
              <button
                className="icon-button"
                aria-label={
                  spoken ? "Disable spoken replies" : "Enable spoken replies"
                }
                onClick={() => {
                  setSpoken(!spoken);
                  if (spoken) void stop();
                }}
              >
                {spoken ? <Volume2 size={17} /> : <VolumeX size={17} />}
              </button>
              {busy && (
                <button className="stop-button" onClick={() => void stop()}>
                  <Square size={12} /> Stop
                </button>
              )}
              <span className="composer-hint">
                {handsFree
                  ? "Hands-free listening"
                  : "You control the microphone"}
              </span>
            </div>
          </div>
          <footer>
            {caps?.stt_provider === "browser"
              ? "Browser recognition may use your browser provider’s servers."
              : "Speech is processed on your voice server."}{" "}
            <span>AI responses can be mistaken.</span>
          </footer>
        </section>
      </main>
      {settingsOpen && (
        <>
          <div
            className="drawer-backdrop"
            onClick={() => setSettingsOpen(false)}
          />
          <aside className="settings-drawer">
            <div className="drawer-title">
              <h2>Conversation settings</h2>
              <button
                className="icon-button"
                aria-label="Close settings"
                onClick={() => setSettingsOpen(false)}
              >
                <X size={20} />
              </button>
            </div>
            <label className="field-label" htmlFor="model">
              Language model
            </label>
            <div className="select-wrap">
              <select
                id="model"
                value={
                  session?.pending_profile ||
                  session?.profile_id ||
                  caps?.default_profile
                }
                onChange={(e) => void changeProfile(e.target.value)}
              >
                {caps?.profiles.map((p) => (
                  <option key={p.id} value={p.id} disabled={!p.available}>
                    {p.label}
                    {p.available
                      ? p.paid
                        ? " · paid provider"
                        : ""
                      : " · configure key"}
                  </option>
                ))}
              </select>
              <ChevronDown size={16} />
            </div>
            <p className="field-help">
              {session?.pending_profile
                ? "Model change queued for the next turn."
                : "Choose the model for this conversation."}
            </p>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={switchImmediately}
                onChange={(e) => setSwitchImmediately(e.target.checked)}
              />{" "}
              Interrupt the current reply when switching
            </label>
            <hr />
            <label className="checkbox">
              <input
                type="checkbox"
                checked={spoken}
                onChange={(e) => setSpoken(e.target.checked)}
              />{" "}
              Read responses aloud
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={handsFree}
                disabled={caps?.stt_provider === "browser" || recording}
                onChange={(e) => setHandsFree(e.target.checked)}
              />{" "}
              Hands-free turn detection
            </label>
            <p className="field-help">
              {caps?.stt_provider !== "browser"
                ? "When enabled, pauses end your turn and speaking interrupts a reply."
                : "Hands-free mode requires the self-hosted audio backend."}
            </p>
            <hr />
            <div className="storage-card">
              <span className="eyebrow">STORAGE</span>
              <strong>
                {caps?.database === "postgresql"
                  ? "PostgreSQL"
                  : "SQLite · demo mode"}
              </strong>
              <p>
                Conversations persist across restarts. Active context stays in
                memory.
              </p>
            </div>
            <p className="field-help">
              {caps?.mcp_servers.length
                ? `${caps.mcp_servers.length} configured MCP server(s)`
                : "Native tools are available on tool-capable models. MCP servers can be configured on the backend."}
            </p>
            <button
              className="reconnect"
              onClick={() => session && void openSession(session)}
            >
              Reconnect this conversation
            </button>
            <button
              className="close-session"
              onClick={async () => {
                if (!session) return;
                try {
                  await client.close(session.id);
                  setSessions((previous) =>
                    previous.filter((s) => s.id !== session.id),
                  );
                  await newSession();
                  setSettingsOpen(false);
                } catch (e) {
                  fail(e);
                }
              }}
            >
              Close this conversation
            </button>
          </aside>
        </>
      )}
      {needsKey && (
        <div className="modal-backdrop">
          <form
            className="access-modal"
            onSubmit={(e) => {
              e.preventDefault();
              setClient(new VoiceClient("", accessKey));
              setAccessKey("");
            }}
          >
            <KeyRound size={26} />
            <h2>Connect to your workspace</h2>
            <p>Enter the access key configured on your voice server.</p>
            <input
              aria-label="Server access key"
              type="password"
              autoFocus
              value={accessKey}
              onChange={(e) => setAccessKey(e.target.value)}
              required
            />
            <button className="mic-button" type="submit">
              Connect
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
