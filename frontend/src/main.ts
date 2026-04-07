/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * and audio playback. Wake-word listening stays in the native helper.
 */

import { createOrb, type OrbState } from "./orb";
import { createAudioPlayer, createVoiceInput, type VoiceInput } from "./voice";
import { createSocket, type SocketLifecycleEvent } from "./ws";
import { openSettings, closeSettings, checkFirstTimeSetup, isSettingsOpen } from "./settings";
import "./style.css";

type State = "idle" | "listening" | "thinking" | "working" | "speaking";

interface PersistedUIState {
  settingsOpen: boolean;
  statusPanelOpen: boolean;
  micRequested: boolean;
  activeMode: string;
  lastFrontendState: State;
  helperConnectionStatus: string;
  lastSavedAt: number;
}

const UI_STATE_KEY = "jarvis.ui-state.v1";
const SESSION_ID_KEY = "jarvis.browser-session-id.v1";
const FRONTEND_RECOVERY_KEY = "jarvis.frontend-recovery.v1";
const SESSION_RESTORE_TIMEOUT_MS = 4000;
const BACKEND_HEALTH_TIMEOUT_MS = 3000;
const RECONNECT_WATCHDOG_MS = 8000;
const STALE_SESSION_FAILURE_LIMIT = 3;
const HARD_RESET_COOLDOWN_MS = 15_000;
const TURN_REQUEST_TIMEOUT_MS = 60_000;  // Increased from 20s to 60s for heavy tasks
const TURN_RESPONSE_TIMEOUT_MS = 45_000; // Increased from 18s to 45s for complex responses

function defaultUiState(): PersistedUIState {
  return {
    settingsOpen: false,
    statusPanelOpen: false,
    micRequested: false, // Default to false; only native helper owns the mic
    activeMode: "conversation",
    lastFrontendState: "idle",
    helperConnectionStatus: "DISCONNECTED",
    lastSavedAt: 0,
  };
}

function loadOrCreateSessionId(): string {
  const existing = window.localStorage.getItem(SESSION_ID_KEY)?.trim();
  if (existing) return existing;
  const created = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  window.localStorage.setItem(SESSION_ID_KEY, created);
  return created;
}

function setSessionId(nextSessionId: string) {
  window.localStorage.setItem(SESSION_ID_KEY, nextSessionId);
}

function loadUiState(): PersistedUIState {
  try {
    const raw = window.localStorage.getItem(UI_STATE_KEY);
    if (!raw) return defaultUiState();
    return { ...defaultUiState(), ...JSON.parse(raw) };
  } catch {
    window.localStorage.removeItem(UI_STATE_KEY);
    return defaultUiState();
  }
}

function loadRecoveryIntent(): { shouldResumeMic: boolean } {
  try {
    const raw = window.sessionStorage.getItem(FRONTEND_RECOVERY_KEY);
    if (!raw) return { shouldResumeMic: false };
    window.sessionStorage.removeItem(FRONTEND_RECOVERY_KEY);
    const data = JSON.parse(raw);
    if (!data?.savedAt || (Date.now() - Number(data.savedAt)) > 20_000) {
      return { shouldResumeMic: false };
    }
    return { shouldResumeMic: !!data.shouldResumeMic };
  } catch {
    window.sessionStorage.removeItem(FRONTEND_RECOVERY_KEY);
    return { shouldResumeMic: false };
  }
}

let currentState: State = "idle";
let isMuted = false;
let voiceInput: VoiceInput | null = null;
let micListening = true;
let turnInFlight = false;
let lastSubmittedText = "";
let lastSubmittedAt = 0;
let browserSessionId = loadOrCreateSessionId();
let persistedUiState = { ...loadUiState(), micRequested: true, activeMode: "conversation" };
let sessionRestoreFailures = 0;
let reconnectWatchdogTimer: ReturnType<typeof setTimeout> | null = null;
let recoveryTimer: ReturnType<typeof setTimeout> | null = null;
let lastHardResetAt = 0;
let hardResetQueued = false;
let backendRestartDetectedAt = 0;
let pendingMicResume = false;
let voiceRecoveryAttempts = 0;
let lastSocketRegistration = "";
let pendingTurnToken = "";
let expectedStreamingTurnId = "";
let activePlaybackTurnId = "";
let lastAudioChunkIndex = -1;
let queuedTranscript = "";
let activeTurnWatchdog: ReturnType<typeof setTimeout> | null = null;
let activeHighPowerToolLabel = "";
const recentTranscriptTokens = new Map<string, number>();

const statusEl = document.getElementById("status-text")!;
const errorEl = document.getElementById("error-text")!;
const contextEl = document.createElement("div");
contextEl.id = "context-snapshot";
contextEl.style.cssText = "position:fixed; bottom:56px; left:50%; transform:translateX(-50%); color:#5af; font-size:12px; font-family:monospace; padding:6px 12px; border-radius:12px; background:rgba(0,0,0,0.6); text-align:center; z-index:2000; opacity:0.5; max-width:80%;";
document.body.appendChild(contextEl);

const missionEl = document.createElement("div");
missionEl.id = "mission-status";
missionEl.style.cssText = "position:fixed; bottom:32px; left:50%; transform:translateX(-50%); color:#daf; font-size:12px; font-family:monospace; padding:4px 10px; border-radius:10px; background:rgba(20,20,40,0.8); text-align:center; z-index:2000; opacity:0.5;";
missionEl.textContent = "";
document.body.appendChild(missionEl);

function nativeHelperActive(): boolean {
  return persistedUiState.helperConnectionStatus === "ACTIVE";
}

function persistUiState(partial: Partial<PersistedUIState> = {}, keepalive = false) {
  persistedUiState = {
    ...persistedUiState,
    ...partial,
    lastSavedAt: Date.now(),
  };
  window.localStorage.setItem(UI_STATE_KEY, JSON.stringify(persistedUiState));

  const payload = JSON.stringify({
    session_id: browserSessionId,
    source: "browser",
    active_mode: persistedUiState.activeMode,
    ui_state: persistedUiState,
  });

  if (keepalive && navigator.sendBeacon) {
    navigator.sendBeacon("/api/session/state", new Blob([payload], { type: "application/json" }));
    return;
  }

  void fetch("/api/session/state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    keepalive,
  }).catch(() => {});
}

async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit = {}, timeoutMs = SESSION_RESTORE_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

function rotateSessionId(reason: string) {
  browserSessionId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}`;
  setSessionId(browserSessionId);
  sessionRestoreFailures = 0;
  lastSocketRegistration = "";
  console.warn("[session] rotated", { reason, sessionId: browserSessionId });
  registerVoiceSocket(true);
}

function clearReconnectWatchdog() {
  if (!reconnectWatchdogTimer) return;
  clearTimeout(reconnectWatchdogTimer);
  reconnectWatchdogTimer = null;
}

function pruneRecentTranscriptTokens() {
  const cutoff = Date.now() - 10_000;
  for (const [token, seenAt] of recentTranscriptTokens.entries()) {
    if (seenAt < cutoff) {
      recentTranscriptTokens.delete(token);
    }
  }
}

function registerVoiceSocket(force = false) {
  if (!socket.isConnected()) return;
  const registrationKey = `${browserSessionId}:browser`;
  if (!force && lastSocketRegistration === registrationKey) return;
  socket.send({
    type: "register_session",
    session_id: browserSessionId,
    source: "browser",
  });
  lastSocketRegistration = registrationKey;
  console.log("[ws] session_registered", { sessionId: browserSessionId, force });
}

function scheduleReconnectWatchdog(reason: string) {
  clearReconnectWatchdog();
  reconnectWatchdogTimer = setTimeout(() => {
    console.warn("[recovery] reconnect watchdog fired", { reason });
    scheduleFrontendRecovery(`watchdog:${reason}`, true);
  }, RECONNECT_WATCHDOG_MS);
}

function resetVoiceInput(reason: string) {
  if (!voiceInput) return;
  console.warn("[voice] resetting input", { reason });
  try {
    voiceInput.stop();
  } catch {
    // Ignore browser speech teardown issues during recovery.
  }
  voiceInput = null;
}

function explainTurnFailure(status: number, detail: string): string {
  const normalized = detail.toLowerCase();
  if (status === 503 || normalized.includes("assistant_pipeline_failed")) {
    if (normalized.includes("tts") || normalized.includes("audio")) {
      return "JARVIS generated a reply, but audio playback failed.";
    }
    return "JARVIS backend is responding slowly. Retrying voice pipeline.";
  }
  if (status >= 500) {
    return "JARVIS backend is not responding.";
  }
  return "JARVIS could not process that voice input.";
}

function showError(msg: string) {
  errorEl.textContent = msg;
  errorEl.style.opacity = "1";
  setTimeout(() => {
    errorEl.style.opacity = "0";
  }, 5000);
}

function showContextSnapshot(text: string) {
  contextEl.textContent = text;
  contextEl.style.opacity = "1";
  setTimeout(() => {
    contextEl.style.opacity = "0.5";
  }, 4000);
}

function showMissionStatus(stage: string, status: string) {
  if (!stage) {
    missionEl.textContent = "";
    missionEl.style.opacity = "0";
    return;
  }
  missionEl.textContent = `Mission ${status}: ${stage}`;
  missionEl.style.opacity = "1";
  setTimeout(() => {
    if (missionEl.textContent.startsWith("Mission")) {
      missionEl.style.opacity = "0.5";
    }
  }, 4000);
}

function syncMicButton() {
  btnMute.classList.toggle("active", micListening);
  btnMute.setAttribute("aria-pressed", micListening ? "true" : "false");
  btnMute.title = micListening ? "Stop Microphone" : "Start Microphone";
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: "",
    listening: "Listening",
    thinking: "Thinking",
    working: "Working",
    speaking: "Talking",
  };
  const suffix = state === "working" && activeHighPowerToolLabel ? ` (${activeHighPowerToolLabel})` : "";
  statusEl.textContent = labels[state];
  if (statusEl.textContent) {
    statusEl.textContent += suffix;
  } else if (suffix) {
    statusEl.textContent = suffix.trim();
  }
}

function setActiveHighPowerTool(label: string | null) {
  activeHighPowerToolLabel = label || "";
  updateStatus(currentState);
}

const canvas = document.getElementById("orb-canvas") as HTMLCanvasElement;
const orb = createOrb(canvas);

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
const socket = createSocket(WS_URL);

// Connect to wake word signal server (ws://127.0.0.1:8342)
const WAKE_SIGNAL_URL = "ws://127.0.0.1:8342";
let wakeWs: WebSocket | null = null;

function connectWakeSignal() {
  try {
    wakeWs = new WebSocket(WAKE_SIGNAL_URL);
    wakeWs.onopen = () => console.log("[wake] connected to signal server");
    wakeWs.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        const ev = data.event;
        
        if (ev === "wake") {
          console.log("[wake] wake signal received");
          window.focus();
          const inputEl = document.getElementById("jarvis-input");
          if (inputEl) inputEl.focus();
          if (currentState === "idle") transition("listening");
        } else if (["listening", "thinking", "speaking", "idle"].includes(ev)) {
          console.log("[wake] remote state transition:", ev);
          transition(ev as any);
        } else if (typeof ev === "string" && ev.startsWith("v:")) {
          // Volume pulse (0.0 to 1.0)
          const level = parseFloat(ev.slice(2));
          if (!isNaN(level) && orb) {
            // We don't have an AnalyserNode for remote audio,
            // so we'll simulate bass/mid triggers if level is high.
            // (Actually, we'd need to expose a pulse method on the Orb, 
            // but for now, just seeing the state change is 90% of the win).
          }
        }
      } catch (err) {
        console.warn("[wake] parse error:", err);
      }
    };
    wakeWs.onclose = () => {
      console.log("[wake] signal server disconnected");
      wakeWs = null;
      setTimeout(connectWakeSignal, 3000);
    };
    wakeWs.onerror = (err) => console.warn("[wake] signal server error:", err);
  } catch (err) {
    console.warn("[wake] failed to connect:", err);
    setTimeout(connectWakeSignal, 5000);
  }
}

connectWakeSignal();

const audioPlayer = createAudioPlayer();
orb.setAnalyser(audioPlayer.getAnalyser());

function handleSocketLifecycle(event: SocketLifecycleEvent) {
  console.log("[ws]", event.type, event);
  if (event.type === "connected") {
    clearReconnectWatchdog();
    hardResetQueued = false;
    backendRestartDetectedAt = 0;
    registerVoiceSocket(true);
    return;
  }
  if (event.type === "disconnected" || event.type === "timeout") {
    scheduleReconnectWatchdog(event.reason || event.type);
    if (event.attempt >= 4) {
      scheduleFrontendRecovery(`socket_attempt_${event.attempt}`);
    }
    return;
  }
  if (event.type === "reconnect_scheduled") {
    if (event.attempt >= 6) {
      scheduleFrontendRecovery(`socket_attempt_${event.attempt}`);
    }
    return;
  }
  if (event.type === "stalled") {
    scheduleFrontendRecovery(`socket_stalled_${event.attempt}`, true);
    if (event.attempt >= 8) {
      queueHardReload(`socket_stalled_${event.attempt}`);
    }
    return;
  }
  if (event.type === "forced_reconnect") {
    scheduleReconnectWatchdog(event.reason || "forced_reconnect");
  }
}

async function restoreSessionState(fromResume = false) {
  try {
    const resp = await fetchWithTimeout(
      `/api/session/state?source=browser&session_id=${encodeURIComponent(browserSessionId)}`,
      {},
      SESSION_RESTORE_TIMEOUT_MS,
    );
    if (!resp.ok) throw new Error(`session_restore_failed:${resp.status}`);
    const data = await resp.json();
    if (!data?.session_id || typeof data.session_id !== "string") {
      throw new Error("session_restore_invalid_payload");
    }
    sessionRestoreFailures = 0;
    persistedUiState = {
      ...persistedUiState,
      ...(data.ui_state || {}),
      helperConnectionStatus: data.helper_connection_status || persistedUiState.helperConnectionStatus,
      lastSavedAt: Date.now(),
    };
    window.localStorage.setItem(UI_STATE_KEY, JSON.stringify(persistedUiState));

    if (micListening) {
      ensureVoiceInput().start();
      transition("listening");
    } else {
      transition("idle");
    }

    if (persistedUiState.settingsOpen && !isSettingsOpen()) {
      void openSettings();
    } else if (!persistedUiState.settingsOpen && isSettingsOpen()) {
      closeSettings();
    }

    if (statusPanelEl) {
      statusPanelEl.style.display = persistedUiState.statusPanelOpen ? "block" : "none";
      if (persistedUiState.statusPanelOpen) {
        void refreshConnectionStatus();
      }
    }
  } catch (error) {
    sessionRestoreFailures += 1;
    console.warn("[session] restore failed", {
      sessionId: browserSessionId,
      failures: sessionRestoreFailures,
      reason: error instanceof Error ? error.message : String(error),
    });
    if (sessionRestoreFailures >= STALE_SESSION_FAILURE_LIMIT) {
      rotateSessionId("stale_session_restore");
      persistUiState({ micRequested: micListening });
    }
  }
}

async function checkBackendHealth(): Promise<boolean> {
  try {
    const resp = await fetchWithTimeout("/api/health", {}, BACKEND_HEALTH_TIMEOUT_MS);
    if (!resp.ok) return false;
    const data = await resp.json();
    return data?.status === "online";
  } catch {
    return false;
  }
}

function persistRecoveryIntent() {
  try {
    window.sessionStorage.setItem(FRONTEND_RECOVERY_KEY, JSON.stringify({
      shouldResumeMic: micListening,
      savedAt: Date.now(),
    }));
  } catch {
    // Ignore session storage failures during recovery.
  }
}

function queueHardReload(reason: string, rotateSession = false) {
  if (hardResetQueued || (Date.now() - lastHardResetAt) < HARD_RESET_COOLDOWN_MS) return;
  hardResetQueued = true;
  lastHardResetAt = Date.now();
  if (rotateSession) {
    rotateSessionId(`hard_reload:${reason}`);
  }
  persistRecoveryIntent();
  persistUiState({}, true);
  resetVoiceInput(`hard_reload:${reason}`);
  statusEl.textContent = "Recovering";
  console.warn("[recovery] hard reload queued", { reason, rotateSession });
  window.setTimeout(() => window.location.reload(), 900);
}

async function runFrontendRecovery(reason: string) {
  recoveryTimer = null;
  const backendHealthy = await checkBackendHealth();
  console.warn("[recovery] running", { reason, backendHealthy, sessionId: browserSessionId });

  if (!backendHealthy) {
    if (!backendRestartDetectedAt) backendRestartDetectedAt = Date.now();
    updateStatus(currentState);
    scheduleReconnectWatchdog(`backend-unhealthy:${reason}`);
    return;
  }

  backendRestartDetectedAt = 0;
  resetVoiceInput(`frontend_recovery:${reason}`);
  socket.forceReconnect(`frontend_recovery:${reason}`);
  void restoreSessionState(true);
  void refreshConnectionStatus();
}

function scheduleFrontendRecovery(reason: string, immediate = false) {
  if (recoveryTimer) return;
  console.warn("[recovery] scheduled", { reason, immediate });
  recoveryTimer = setTimeout(() => {
    void runFrontendRecovery(reason);
  }, immediate ? 0 : 700);
}

function resumeMicIfNeeded(reason: string) {
  // Browser microphone ownership removed. Native helper handles mic lifecycle.
  console.log("[MIC] browser_mic_init BLOCKED (Native owner only)", { reason });
  micListening = false;
  pendingMicResume = false;
  syncMicButton();
}

function transition(newState: State) {
  if (newState === currentState) return;
  currentState = newState;
  const orbState: OrbState = (newState === "working" || newState === "speaking") ? "speaking" : (newState as OrbState);
  orb.setState(orbState);
  updateStatus(newState);
  persistUiState({ lastFrontendState: newState });
}

function clearTurnWatchdog() {
  if (!activeTurnWatchdog) return;
  clearTimeout(activeTurnWatchdog);
  activeTurnWatchdog = null;
}

function flushQueuedTranscript(reason: string) {
  const next = queuedTranscript.trim();
  if (!next || turnInFlight) return;
  queuedTranscript = "";
  console.log("[turn] flushing queued transcript", { reason, chars: next.length });
  void submitTurn(next);
}

function recoverToListening(reason: string, message: string, recoverBackend = false) {
  console.warn("[turn] recovering", { reason, recoverBackend });
  clearTurnWatchdog();
  pendingTurnToken = "";
  expectedStreamingTurnId = "";
  activePlaybackTurnId = "";
  lastAudioChunkIndex = -1;
  turnInFlight = false;
  audioPlayer.stop();
  showError(message);
  if (recoverBackend) {
    scheduleFrontendRecovery(reason, true);
  }
  if (micListening && voiceInput) {
    voiceInput.resume();
    transition("listening");
  } else if (micListening || pendingMicResume) {
    resumeMicIfNeeded(reason);
  } else {
    transition("idle");
  }
}

function armTurnWatchdog(reason: string, timeoutMs = TURN_RESPONSE_TIMEOUT_MS) {
  clearTurnWatchdog();
  activeTurnWatchdog = setTimeout(() => {
    recoverToListening(reason, "JARVIS heard you, but the reply stalled. Recovering now.", true);
  }, timeoutMs);
}

async function submitTurn(text: string) {
  const userText = text.trim();
  if (!userText) return;
  if (turnInFlight) {
    queuedTranscript = userText;
    transition("working");
    armTurnWatchdog("queued_turn_waiting");
    return;
  }
  const normalizedText = userText.toLowerCase().replace(/\s+/g, " ").trim();
  const now = Date.now();
  pruneRecentTranscriptTokens();
  const transcriptToken = `${browserSessionId}:${normalizedText}`;
  if (normalizedText && normalizedText === lastSubmittedText && (now - lastSubmittedAt) < 1500) return;
  if (recentTranscriptTokens.has(transcriptToken) && (now - (recentTranscriptTokens.get(transcriptToken) || 0)) < 1500) return;
  turnInFlight = true;
  lastSubmittedText = normalizedText;
  lastSubmittedAt = now;
  recentTranscriptTokens.set(transcriptToken, now);
  pendingTurnToken = transcriptToken;
  expectedStreamingTurnId = "";
  activePlaybackTurnId = "";
  lastAudioChunkIndex = -1;
  audioPlayer.stop();
  console.log("[voice] transcript_submitted", { chars: userText.length, sessionId: browserSessionId });
  transition("thinking");
  armTurnWatchdog("assistant_turn_request", TURN_REQUEST_TIMEOUT_MS);
  try {
    const resp = await fetchWithTimeout("/api/assistant/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: userText, session_id: browserSessionId, source: "browser" }),
    }, TURN_REQUEST_TIMEOUT_MS);
    if (!resp.ok) {
      const payload = await resp.text().catch(() => "");
      throw new Error(`turn_failed:${resp.status}:${payload}`);
    }
    const data = await resp.json();
    if (data.status === "deduped") {
      pendingTurnToken = "";
      clearTurnWatchdog();
      transition(micListening ? "listening" : "idle");
      flushQueuedTranscript("deduped_turn_complete");
      return;
    }
    if (data.audio && !isMuted) {
      pendingTurnToken = "";
      expectedStreamingTurnId = data.turn_id || "";
      activePlaybackTurnId = data.turn_id || "";
      lastAudioChunkIndex = -1;
      clearTurnWatchdog();
      transition("working");
      void audioPlayer.enqueue(data.audio).catch((error) => {
        console.warn("[audio] playback enqueue failed", error);
        recoverToListening("direct_audio_enqueue_failed", "JARVIS generated a reply, but audio playback failed.");
      });
    } else if (data.tts_pending) {
      expectedStreamingTurnId = data.turn_id || "";
      activePlaybackTurnId = "";
      lastAudioChunkIndex = -1;
      transition("thinking");
      armTurnWatchdog(`tts_pending:${data.turn_id || "unknown"}`);
      console.log("[turn] tts pending", { turnId: data.turn_id, sessionId: browserSessionId });
    } else {
      pendingTurnToken = "";
      if (data.text && !data.audio) {
        recoverToListening("no_audio_available", "JARVIS replied, but no audio was available.");
        return;
      }
      clearTurnWatchdog();
      transition(micListening ? "listening" : "idle");
      flushQueuedTranscript("turn_complete_without_audio");
    }
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    console.warn("[turn] submission failed", {
      sessionId: browserSessionId,
      reason,
    });
    const [, statusToken = "0", detail = ""] = reason.split(":", 3);
    const statusCode = Number(statusToken) || 0;
    recoverToListening("assistant_turn_failed", explainTurnFailure(statusCode, detail), true);
  } finally {
    turnInFlight = false;
  }
}

function ensureVoiceInput(): VoiceInput {
  if (voiceInput) return voiceInput;
  voiceInput = createVoiceInput(
    (text) => { void submitTurn(text); },
    (msg, reason) => {
       console.warn("[voice] browser input event blocked (UI only)", { msg, reason });
    },
  );
  return voiceInput;
}

audioPlayer.onFinished(() => {
  clearTurnWatchdog();
  activePlaybackTurnId = "";
  lastAudioChunkIndex = -1;
  voiceRecoveryAttempts = 0;
  if (micListening && voiceInput) {
    voiceInput.resume();
    transition("listening");
  } else {
    transition("idle");
  }
  flushQueuedTranscript("audio_finished");
});

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "audio") {
    const audioData = msg.data as string;
    const turnId = typeof msg.turn_id === "string" ? msg.turn_id : "";
    const chunkIndex = typeof msg.chunk_index === "number" ? msg.chunk_index : -1;
    if (expectedStreamingTurnId && turnId && turnId !== expectedStreamingTurnId) {
      console.warn("[audio] stale turn ignored", { turnId, expectedStreamingTurnId });
      return;
    }
    if (turnId && activePlaybackTurnId && turnId !== activePlaybackTurnId) {
      audioPlayer.stop();
      lastAudioChunkIndex = -1;
    }
    if (turnId && chunkIndex >= 0 && turnId === activePlaybackTurnId && chunkIndex <= lastAudioChunkIndex) {
      console.warn("[audio] duplicate chunk ignored", { turnId, chunkIndex, lastAudioChunkIndex });
      return;
    }
    pendingTurnToken = "";
    if (turnId) {
      activePlaybackTurnId = turnId;
      expectedStreamingTurnId = turnId;
    }
    if (chunkIndex >= 0) {
      lastAudioChunkIndex = chunkIndex;
    }
    clearTurnWatchdog();
    console.log("[audio] received", audioData ? `${audioData.length} chars` : "EMPTY", "state:", currentState);
    if (audioData && !isMuted) {
      if (micListening && voiceInput) {
        voiceInput.pause();
      }
      if (currentState !== "working") {
        transition("speaking");
      }
      void audioPlayer.enqueue(audioData).catch((error) => {
        console.warn("[audio] ws playback enqueue failed", error);
        recoverToListening("stream_audio_enqueue_failed", "JARVIS received audio, but playback failed.");
      });
    } else if (audioData && isMuted) {
      console.log("[audio] muted; skipping playback");
      clearTurnWatchdog();
      transition("working");
    } else {
      console.warn("[audio] no data received, returning to idle");
      recoverToListening("empty_audio_chunk", "JARVIS produced an empty audio chunk. Recovering now.");
      return;
    }
    if (msg.text) console.log("[JARVIS]", msg.text);
  } else if (type === "status") {
    const state = msg.state as string;
    const activity = msg.activity as string | undefined;
    const turnId = typeof msg.turn_id === "string" ? msg.turn_id : "";
    if (expectedStreamingTurnId && turnId && turnId !== expectedStreamingTurnId) {
      return;
    }
    // Update activity label if provided
    if (activity) {
      setActiveHighPowerTool(activity);
    }
    if (state === "listening" && currentState !== "listening") {
      clearTurnWatchdog();
      transition("listening");
    } else if (state === "thinking" && currentState !== "thinking") {
      if (turnId && activePlaybackTurnId && turnId !== activePlaybackTurnId) {
        audioPlayer.stop();
        activePlaybackTurnId = "";
        lastAudioChunkIndex = -1;
      }
      transition("thinking");
      armTurnWatchdog(`status_thinking:${turnId || "unknown"}`);
    } else if (state === "working") {
      transition("working");
      armTurnWatchdog(`status_working:${turnId || "unknown"}`);
    } else if (state === "idle") {
      if (!turnId || !expectedStreamingTurnId || turnId === expectedStreamingTurnId) {
        expectedStreamingTurnId = "";
      }
      clearTurnWatchdog();
      if (!activePlaybackTurnId) {
        lastAudioChunkIndex = -1;
        setActiveHighPowerTool(null);  // Clear activity on idle
        transition(micListening ? "listening" : "idle");
        flushQueuedTranscript("status_idle");
      }
    }
  } else if (type === "text") {
    console.log("[JARVIS]", msg.text);
  } else if (type === "tts_failed") {
    pendingTurnToken = "";
    console.warn("[audio] background tts failed", msg);
    recoverToListening("tts_failed", "JARVIS replied, but audio playback failed.");
  } else if (type === "context_snapshot") {
    showContextSnapshot(msg.snapshot as string);
  } else if (type === "mission_update") {
    const stage = msg.stage as string;
    const status = msg.status as string;
    showMissionStatus(stage, status);
  } else if (type === "session_registered") {
    console.log("[ws] backend acknowledged session registration", msg);
  } else if (type === "task_spawned") {
    console.log("[task]", "spawned:", msg.task_id, msg.prompt);
    transition("working");
    setActiveHighPowerTool(msg.tool as string | null);
    armTurnWatchdog(`task_spawned:${msg.task_id || "unknown"}`);
  } else if (type === "task_complete") {
    console.log("[task]", "complete:", msg.task_id, msg.status, msg.summary);
    clearTurnWatchdog();
    setActiveHighPowerTool(null);
    transition(micListening ? "listening" : "idle");
    flushQueuedTranscript("task_complete");
  }
});

socket.onLifecycle(handleSocketLifecycle);

socket.onConnectionChange((connected) => {
  if (connected) {
    clearReconnectWatchdog();
    clearTurnWatchdog();
    registerVoiceSocket(true);
    void restoreSessionState(document.visibilityState === "visible");
    if (micListening && voiceInput) {
      voiceInput.resume();
      transition("listening");
    } else if (micListening || pendingMicResume) {
      resumeMicIfNeeded("socket_connected");
    }
    return;
  }
  clearTurnWatchdog();
  if (document.visibilityState === "visible") updateStatus(currentState);
  scheduleReconnectWatchdog("socket_disconnected");
});

setTimeout(() => {
  transition("idle");
}, 250);

function ensureAudioContext() {
  const ctx = audioPlayer.getAnalyser().context as AudioContext;
  if (ctx.state === "suspended") {
    ctx.resume().then(() => console.log("[audio] context resumed")).catch(() => {
      showError("Audio playback is blocked until the page receives interaction.");
    });
  }
}
document.addEventListener("click", ensureAudioContext);
document.addEventListener("touchstart", ensureAudioContext);
document.addEventListener("keydown", ensureAudioContext, { once: true });
ensureAudioContext();

const btnMute = document.getElementById("btn-mute")!;
const btnMenu = document.getElementById("btn-menu")!;
const menuDropdown = document.getElementById("menu-dropdown")!;
const btnRestart = document.getElementById("btn-restart")!;
const btnFixSelf = document.getElementById("btn-fix-self")!;

function toggleMic(e?: Event) {
  e?.stopPropagation();
  micListening = !micListening;
  pendingMicResume = false;
  syncMicButton();
  if (micListening) {
    voiceRecoveryAttempts = 0;
    if (!socket.isConnected()) {
      scheduleFrontendRecovery("mic_requested_while_disconnected", true);
    }
    const input = ensureVoiceInput();
    input.start();
    input.setActive(true);
    persistedUiState.activeMode = "conversation";
    persistUiState({ micRequested: true, activeMode: "conversation" });
    transition("listening");
  } else {
    if (voiceInput) {
      voiceInput.stop();
    }
    voiceRecoveryAttempts = 0;
    persistUiState({ micRequested: false });
    transition("idle");
  }
}

btnMute.addEventListener("click", toggleMic);

syncMicButton();
updateStatus(currentState);

btnMenu.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = menuDropdown.style.display === "none" ? "block" : "none";
});

document.addEventListener("click", () => {
  menuDropdown.style.display = "none";
});

btnRestart.addEventListener("click", async (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  persistUiState({}, true);
  statusEl.textContent = "Working";
  try {
    await fetch("/api/restart", { method: "POST" });
    setTimeout(() => window.location.reload(), 4000);
  } catch {
    updateStatus(currentState);
  }
});

btnFixSelf.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  persistUiState({ activeMode: "work" });
  socket.send({ type: "fix_self" });
  transition("thinking");
  statusEl.textContent = "Working";
});

const btnSettings = document.getElementById("btn-settings")!;
btnSettings.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  openSettings();
});

setTimeout(() => {
  checkFirstTimeSetup();
}, 2000);

const STATUS_LABELS: Record<string, string> = {
  claude: "Claude Code",
  cloudcode: "Claude Cowork",
  ct: "CT",
  mistral: "Mistral",
  mistral_chat: "Mistral",
  mistral_code: "Codestral",
  codex: "Codex",
  opencode: "OpenCode",
  edge_tts: "Edge TTS (RyanNeural)",
  microphone: "Microphone",
  wake_word: "Wake Word Engine",
  comet_browser: "Comet Browser",
  speckit: "SpecKit",
  localai: "LocalAI",
  antigravity: "AntiGravity",
  local_system: "Local System",
  background_service: "Background Service",
  file_system: "File System Access",
  memory_system: "Memory System",
  applescript: "AppleScript Access",
};

let statusPanelEl: HTMLElement | null = null;

function buildStatusPanel(): HTMLElement {
  const panel = document.createElement("div");
  panel.id = "connection-status-panel";
  panel.style.cssText = `
    position: fixed; top: 20px; left: 20px; z-index: 9999;
    background: rgba(0,0,0,0.85); border: 1px solid #1a3a5c;
    border-radius: 12px; padding: 16px 20px; min-width: 260px;
    font-family: -apple-system, system-ui, monospace; font-size: 12px;
    color: #ccc; backdrop-filter: blur(12px); display: none;
    box-shadow: 0 4px 24px rgba(0,150,255,0.15);
  `;
  panel.innerHTML = `
    <div style="font-size:13px;font-weight:600;color:#4fc3f7;margin-bottom:10px;letter-spacing:.05em;">
      ⬡ JARVIS CONNECTIONS
    </div>
    <div id="status-rows"></div>
    <div style="margin-top:10px;border-top:1px solid #1a3a5c;padding-top:8px;font-size:10px;color:#555;">
      Updated every 30s · Click to close
    </div>
  `;
  panel.addEventListener("click", () => {
    panel.style.display = "none";
    persistUiState({ statusPanelOpen: false });
  });
  document.body.appendChild(panel);
  return panel;
}

function renderStatusRows(connections: Record<string, string>) {
  const container = document.getElementById("status-rows");
  if (!container) return;
  container.innerHTML = "";
  for (const [key, raw] of Object.entries(connections)) {
    const label = STATUS_LABELS[key] || key;
    let status = raw;
    let dot = "";
    let color = "";
    if (raw === "CONNECTED" || raw === "ACTIVE") {
      dot = "●";
      color = "#4caf50";
    } else if (raw === "AUTH_REQUIRED" || raw === "INSTALLED" || raw === "RATE_LIMITED" || raw === "DISABLED" || raw === "LOW_DISK") {
      dot = "●";
      color = raw === "DISABLED" ? "#90caf9" : "#ffa726";
    } else {
      dot = "○";
      color = "#ef5350";
    }
    const row = document.createElement("div");
    row.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin:4px 0;";
    row.innerHTML = `
      <span style="color:#aaa;">${label}</span>
      <span style="color:${color};font-weight:600;font-size:11px;">${dot} ${status}</span>
    `;
    container.appendChild(row);
  }
}

async function refreshConnectionStatus() {
  try {
    const resp = await fetch("/api/connections");
    if (!resp.ok) return;
    const data = await resp.json();
    persistUiState({
      helperConnectionStatus: data.connections?.microphone ?? persistedUiState.helperConnectionStatus,
    });
    if (statusPanelEl) renderStatusRows(data.connections);
  } catch {
    // Server not ready
  }
}

function addStatusButton() {
  const menu = document.getElementById("menu-dropdown");
  if (!menu) return;
  const btn = document.createElement("button");
  btn.id = "btn-status";
  btn.textContent = "Connection Status";
  btn.style.cssText = `display:block;width:100%;padding:8px 12px;background:none;
    border:none;color:#e0e0e0;font-size:13px;cursor:pointer;text-align:left;`;
  btn.addEventListener("mouseenter", () => { btn.style.background = "rgba(79,195,247,0.1)"; });
  btn.addEventListener("mouseleave", () => { btn.style.background = "none"; });
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.style.display = "none";
    if (!statusPanelEl) statusPanelEl = buildStatusPanel();
    statusPanelEl.style.display = statusPanelEl.style.display === "none" ? "block" : "none";
    persistUiState({ statusPanelOpen: statusPanelEl.style.display === "block" });
    if (statusPanelEl.style.display === "block") refreshConnectionStatus();
  });
  menu.insertBefore(btn, menu.firstChild);
}

setTimeout(() => {
  addStatusButton();
  statusPanelEl = buildStatusPanel();
  if (persistedUiState.statusPanelOpen) {
    statusPanelEl.style.display = "block";
    void refreshConnectionStatus();
  }
  setInterval(() => {
    void refreshConnectionStatus();
  }, 10_000);
}, 1500);

document.addEventListener("jarvis:settings-visibility", ((event: Event) => {
  const detail = (event as CustomEvent<{ open?: boolean }>).detail;
  persistUiState({ settingsOpen: !!detail?.open });
}) as EventListener);

window.addEventListener("pageshow", () => {
  void restoreSessionState(true);
  void refreshConnectionStatus();
  if (!socket.isConnected()) {
    scheduleFrontendRecovery("pageshow_resume", true);
  } else if (micListening && voiceInput) {
    voiceInput.resume();
  }
});

window.addEventListener("online", () => {
  void restoreSessionState(true);
  void refreshConnectionStatus();
  scheduleFrontendRecovery("browser_online", true);
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") {
    persistUiState({}, true);
    return;
  }
  void restoreSessionState(true);
  void refreshConnectionStatus();
  if (!socket.isConnected()) {
    scheduleFrontendRecovery("visibility_resume", true);
  } else if (micListening && voiceInput) {
    voiceInput.resume();
  }
});

window.addEventListener("beforeunload", () => persistUiState({}, true));
window.addEventListener("pagehide", () => persistUiState({}, true));

// Singleton Dashboard Heartbeat and Duplicate Prevention
async function startDashboardHeartbeat() {
  console.log("[session] starting singleton heartbeat", { sessionId: browserSessionId });
  const payload = {
    session_id: browserSessionId,
    browser: "Comet",
    is_visible: document.visibilityState === "visible"
  };

  try {
    const regResp = await fetch("/api/dashboard/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const regData = await regResp.json();
    
    if (regData.state === "duplicate") {
      console.warn("[session] duplicate dashboard detected");
      showError("JARVIS is already active in another tab.");
      transition("idle");
      micListening = false;
      syncMicButton();
      orb.setState("idle");
      return; 
    }

    setInterval(async () => {
      try {
        const hbResp = await fetch("/api/dashboard/heartbeat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...payload, is_visible: document.visibilityState === "visible" })
        });
        const hbData = await hbResp.json();
        if (hbData.state === "duplicate") {
          window.location.reload(); 
        }
      } catch (err) {
        // Heartbeat failure ignored
      }
    }, 10000); 

  } catch (err) {
    console.warn("[session] heartbeat init failed", err);
  }
}

void startDashboardHeartbeat();
void restoreSessionState(false);
void refreshConnectionStatus();
void checkFirstTimeSetup();
