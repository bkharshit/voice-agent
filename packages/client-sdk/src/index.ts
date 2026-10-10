export type Profile = {
  id: string;
  label: string;
  model: string;
  available: boolean;
  paid: boolean;
  tool_calling: boolean;
};
export type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  generation_id: string;
  status: string;
  voice: boolean;
  delivered_chars: number;
};
export type Session = {
  id: string;
  profile_id: string;
  revision: number;
  pending_profile: string | null;
  status: string;
  updated_at: string;
  messages?: Message[];
  output?: string;
  generation_id?: string | null;
};
export type Capabilities = {
  mode: string;
  database: string;
  profiles: Profile[];
  default_profile: string;
  audio_backend: "browser" | "local";
  stt_provider: string;
  tts_provider: "browser" | "kokoro";
  mcp_servers: { id: string; tools: string[] }[];
};
export type AgentEvent = {
  schema_version: number;
  type: string;
  session_id: string;
  turn_id: string | null;
  generation_id: string | null;
  config_revision: number;
  sequence: number;
  data: Record<string, unknown>;
};

export class VoiceClient {
  constructor(
    public baseUrl = "",
    private accessKey = "",
  ) {}
  async request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
    const response = await fetch(this.baseUrl + path, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(this.accessKey
          ? { Authorization: `Bearer ${this.accessKey}` }
          : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      const error = await response
        .json()
        .catch(() => ({ detail: response.statusText }));
      throw new Error(
        typeof error.detail === "string" ? error.detail : "Invalid request",
      );
    }
    return response.status === 204 ? (undefined as T) : response.json();
  }
  capabilities() {
    return this.request<Capabilities>("/v1/capabilities");
  }
  sessions() {
    return this.request<Session[]>("/v1/sessions");
  }
  create(profile_id: string) {
    return this.request<Session>("/v1/sessions", "POST", { profile_id });
  }
  snapshot(id: string) {
    return this.request<Session>(`/v1/sessions/${id}`);
  }
  turn(id: string, text: string, voice: boolean) {
    return this.request<{ generation_id: string }>(
      `/v1/sessions/${id}/turns`,
      "POST",
      { text, voice },
    );
  }
  configure(session: Session, llm_profile: string, immediate = false) {
    return this.request<Session>(
      `/v1/sessions/${session.id}/configuration`,
      "PATCH",
      {
        llm_profile,
        expected_revision: session.revision,
        apply: immediate ? "interrupt_current" : "next_turn",
      },
    );
  }
  interrupt(id: string, generation_id: string | null) {
    return this.request(`/v1/sessions/${id}/interrupt`, "POST", {
      generation_id,
    });
  }
  receipt(id: string, generation_id: string, chars: number, complete = false) {
    return this.request(`/v1/sessions/${id}/playback`, "POST", {
      generation_id,
      chars,
      complete,
    });
  }
  close(id: string) {
    return this.request(`/v1/sessions/${id}`, "DELETE");
  }
  async connect(id: string): Promise<WebSocket> {
    const connection = await this.request<{ path: string; ticket: string }>(
      `/v1/sessions/${id}/connection`,
      "POST",
      {},
    );
    const url = new URL(
      connection.path,
      this.baseUrl || window.location.origin,
    );
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("ticket", connection.ticket);
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(url);
      const timeout = setTimeout(() => {
        socket.close();
        reject(new Error("Session connection timed out. Please reconnect."));
      }, 15000);
      const fail = (message: string) => {
        clearTimeout(timeout);
        reject(new Error(message));
      };
      socket.onmessage = (event) => {
        try {
          if (JSON.parse(event.data).type !== "connected") return;
          clearTimeout(timeout);
          socket.onmessage = null;
          socket.onerror = null;
          socket.onclose = null;
          resolve(socket);
        } catch {
          fail("Invalid session handshake");
          socket.close();
        }
      };
      socket.onerror = () => fail("Could not connect to session");
      socket.onclose = (event) =>
        fail(event.reason || "Session closed before connecting. Please retry.");
    });
  }
}
