/**
 * WebSocket client for JARVIS server communication.
 */

export type MessageHandler = (msg: Record<string, unknown>) => void;

export interface SocketLifecycleEvent {
  type: "connecting" | "connected" | "disconnected" | "reconnect_scheduled" | "timeout" | "stalled" | "forced_reconnect";
  attempt: number;
  reason?: string;
  delayMs?: number;
  code?: number;
}

export interface JarvisSocket {
  send(data: Record<string, unknown>): void;
  onMessage(handler: MessageHandler): void;
  onConnectionChange(handler: (connected: boolean) => void): void;
  onLifecycle(handler: (event: SocketLifecycleEvent) => void): void;
  forceReconnect(reason?: string): void;
  close(): void;
  isConnected(): boolean;
}

export function createSocket(url: string): JarvisSocket {
  const CONNECT_TIMEOUT_MS = 5000;
  const MAX_RECONNECT_DELAY_MS = 15000;
  const STALLED_ATTEMPTS = 6;

  let ws: WebSocket | null = null;
  let handlers: MessageHandler[] = [];
  let connectionHandlers: Array<(connected: boolean) => void> = [];
  let lifecycleHandlers: Array<(event: SocketLifecycleEvent) => void> = [];
  let reconnectDelay = 1000;
  let closed = false;
  let connected = false;
  let connectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let socketGeneration = 0;
  let reconnectAttempts = 0;
  let lastConnectStartedAt = 0;

  function emitLifecycle(event: SocketLifecycleEvent) {
    for (const handler of lifecycleHandlers) handler(event);
  }

  function emitConnectionState(nextConnected: boolean) {
    if (connected === nextConnected) return;
    connected = nextConnected;
    for (const handler of connectionHandlers) handler(nextConnected);
  }

  function clearConnectTimer() {
    if (!connectTimer) return;
    clearTimeout(connectTimer);
    connectTimer = null;
  }

  function clearReconnectTimer() {
    if (!reconnectTimer) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function disconnectSocket(reason: string) {
    if (!ws) return;
    const active = ws;
    ws = null;
    active.onopen = null;
    active.onmessage = null;
    active.onerror = null;
    active.onclose = null;
    if (active.readyState === WebSocket.OPEN || active.readyState === WebSocket.CONNECTING) {
      try {
        active.close(1000, reason.slice(0, 60));
      } catch {
        // Ignore close failures while replacing stale sockets.
      }
    }
  }

  function scheduleReconnect(reason: string, immediate = false) {
    if (closed || reconnectTimer) return;
    const jitter = Math.floor(Math.random() * 200);
    const delayMs = immediate ? 0 : Math.min(reconnectDelay + jitter, MAX_RECONNECT_DELAY_MS);
    reconnectAttempts += 1;
    emitLifecycle({ type: "reconnect_scheduled", attempt: reconnectAttempts, reason, delayMs });
    if (reconnectAttempts >= STALLED_ATTEMPTS) {
      emitLifecycle({ type: "stalled", attempt: reconnectAttempts, reason });
    }
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect(reason);
    }, delayMs);
    reconnectDelay = Math.min(Math.round(reconnectDelay * 1.8), MAX_RECONNECT_DELAY_MS);
  }

  function connect(reason = "initial") {
    if (closed) return;
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    disconnectSocket("replace");
    clearConnectTimer();
    clearReconnectTimer();

    const generation = ++socketGeneration;
    lastConnectStartedAt = Date.now();
    emitLifecycle({ type: "connecting", attempt: reconnectAttempts, reason });

    ws = new WebSocket(url);
    const activeSocket = ws;
    connectTimer = setTimeout(() => {
      if (ws !== activeSocket || generation !== socketGeneration || closed) return;
      emitLifecycle({ type: "timeout", attempt: reconnectAttempts, reason: "connect_timeout", delayMs: CONNECT_TIMEOUT_MS });
      disconnectSocket("connect-timeout");
      emitConnectionState(false);
      scheduleReconnect("connect-timeout");
    }, CONNECT_TIMEOUT_MS);

    ws.onopen = () => {
      if (generation !== socketGeneration || ws !== activeSocket) return;
      clearConnectTimer();
      emitConnectionState(true);
      reconnectDelay = 1000;
      reconnectAttempts = 0;
      clearReconnectTimer();
      emitLifecycle({ type: "connected", attempt: 0, reason });
      console.log("[ws] connected");
    };

    ws.onmessage = (event) => {
      if (generation !== socketGeneration || ws !== activeSocket) return;
      try {
        const msg = JSON.parse(event.data);
        for (const h of handlers) h(msg);
      } catch {
        console.warn("[ws] bad message", event.data);
      }
    };

    ws.onclose = (event) => {
      if (generation !== socketGeneration || activeSocket !== ws) return;
      clearConnectTimer();
      ws = null;
      emitConnectionState(false);
      emitLifecycle({ type: "disconnected", attempt: reconnectAttempts, reason: event.reason || "socket_closed", code: event.code });
      if (!closed) {
        const elapsedMs = Date.now() - lastConnectStartedAt;
        console.log(`[ws] reconnecting in ${reconnectDelay}ms`);
        scheduleReconnect(event.reason || `close:${event.code}:${elapsedMs}`);
      }
    };

    ws.onerror = (err) => {
      if (generation !== socketGeneration || ws !== activeSocket) return;
      console.error("[ws] error", err);
      emitConnectionState(false);
      disconnectSocket("socket-error");
      scheduleReconnect("socket-error");
    };
  }

  connect();

  return {
    send(data) {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(data));
      }
    },
    onMessage(handler) {
      handlers.push(handler);
    },
    onConnectionChange(handler) {
      connectionHandlers.push(handler);
      handler(connected);
    },
    onLifecycle(handler) {
      lifecycleHandlers.push(handler);
    },
    forceReconnect(reason = "forced") {
      if (closed) return;
      emitLifecycle({ type: "forced_reconnect", attempt: reconnectAttempts, reason });
      clearConnectTimer();
      clearReconnectTimer();
      emitConnectionState(false);
      disconnectSocket(reason);
      scheduleReconnect(reason, true);
    },
    close() {
      closed = true;
      clearConnectTimer();
      clearReconnectTimer();
      disconnectSocket("closed");
      emitConnectionState(false);
    },
    isConnected() {
      return connected;
    },
  };
}
