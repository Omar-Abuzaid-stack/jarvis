/**
 * JARVIS — Settings Panel
 *
 * Overlay panel for API keys, connection status, preferences, and system info.
 * Slides in from the right with glass-morphism styling.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface StatusResponse {
  claude_code_installed: boolean;
  calendar_accessible: boolean;
  mail_accessible: boolean;
  notes_accessible: boolean;
  memory_count: number;
  task_count: number;
  server_port: number;
  uptime_seconds: number;
  timezone?: string;
  models?: {
    chat?: string;
    code?: string;
    chat_fallback?: string | null;
    code_fallback?: string | null;
  };
  env_keys_set: {
    mistral: boolean;
    codestral: boolean;
    edge_tts: boolean;
    user_name: string;
  };
}

interface PreferencesResponse {
  user_name: string;
  honorific: string;
  calendar_accounts: string;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let panelEl: HTMLElement | null = null;
let isOpen = false;
let isFirstTimeSetup = false;
let setupStep = 0; // 0=mistral, 1=name, 2=done

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiGet<T>(url: string): Promise<T> {
  const res = await fetch(url);
  return res.json();
}

async function apiPost<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

// ---------------------------------------------------------------------------
// Panel HTML
// ---------------------------------------------------------------------------

function buildPanelHTML(): string {
  return `
    <div class="settings-backdrop" id="settings-backdrop"></div>
    <div class="settings-panel" id="settings-panel-inner">
      <div class="settings-header">
        <h2>Settings</h2>
        <button class="settings-close" id="settings-close">&times;</button>
      </div>

      <div class="settings-welcome" id="settings-welcome" style="display:none">
        <p>Welcome to JARVIS. Let's get you set up.</p>
      </div>

      <div class="settings-body">

        <!-- API Keys -->
        <section class="settings-section" id="section-api-keys">
          <h3>API Keys</h3>

          <div class="settings-field">
            <label>Mistral API Key</label>
            <div class="settings-input-row">
              <input type="password" id="input-mistral-key" placeholder="Mistral API key..." />
              <button class="settings-btn" id="btn-test-mistral">Test</button>
              <span class="status-dot" id="status-mistral"></span>
            </div>
          </div>

          <div class="settings-field">
            <label>Codestral API Key</label>
            <div class="settings-input-row">
              <input type="password" id="input-codestral-key" placeholder="Codestral API key..." />
              <button class="settings-btn" id="btn-test-codestral">Test</button>
              <span class="status-dot" id="status-codestral"></span>
            </div>
          </div>

          <div class="settings-field">
            <label>Voice</label>
            <div class="settings-input-row">
              <select id="input-voice-select" style="flex:1">
                <option value="en-GB-RyanNeural">Ryan (British, default)</option>
                <option value="en-US-GuyNeural">Guy (American)</option>
                <option value="en-AU-WilliamNeural">William (Australian)</option>
                <option value="ar-EG-SalmaNeural">Salma (Egyptian Arabic, female)</option>
                <option value="ar-EG-ShakirNeural">Shakir (Egyptian Arabic, male)</option>
                <option value="ar-AE-FatimaNeural">Fatima (Gulf Arabic, female)</option>
                <option value="ar-SA-HamedNeural">Hamed (Saudi Arabic, male)</option>
                <option value="daniel-macos">Daniel (macOS offline)</option>
              </select>
              <span class="status-dot" id="status-edge-tts"></span>
            </div>
            <small style="color:#888;margin-top:4px;display:block">Edge TTS — no API key required. Fallback: macOS Daniel (offline).</small>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-keys">Save Keys</button>
          </div>
        </section>

        <!-- Connection Status -->
        <section class="settings-section" id="section-status">
          <h3>Connection Status</h3>
          <div class="status-grid">
            <div class="status-row"><span class="status-dot" id="status-server"></span><span>Server</span><span class="status-detail" id="status-server-detail"></span></div>
            <div class="status-row"><span class="status-dot" id="status-background-service"></span><span>Background Service</span></div>
            <div class="status-row"><span class="status-dot" id="status-calendar"></span><span>Apple Calendar</span></div>
            <div class="status-row"><span class="status-dot" id="status-mail"></span><span>Apple Mail</span></div>
            <div class="status-row"><span class="status-dot" id="status-notes"></span><span>Apple Notes</span></div>
            <div class="status-row"><span class="status-dot" id="status-claude-cli"></span><span>CloudCode CLI</span></div>
            <div class="status-row"><span class="status-dot" id="status-speckit"></span><span>SpecKit</span></div>
            <div class="status-row"><span class="status-dot" id="status-antigravity"></span><span>AntiGravity</span></div>
            <div class="status-row"><span class="status-dot" id="status-opencode"></span><span>OpenCode</span></div>
            <div class="status-row"><span class="status-dot" id="status-localai"></span><span>LocalAI</span></div>
            <div class="status-row"><span class="status-dot" id="status-codex"></span><span>Codex</span></div>
            <div class="status-row"><span class="status-dot" id="status-local-system"></span><span>Local System</span></div>
          </div>
        </section>

        <!-- User Preferences -->
        <section class="settings-section" id="section-preferences">
          <h3>User Preferences</h3>

          <div class="settings-field">
            <label>Your Name</label>
            <input type="text" id="input-user-name" placeholder="Your name" />
          </div>

          <div class="settings-field">
            <label>Honorific</label>
            <select id="input-honorific">
              <option value="sir">Sir</option>
              <option value="ma'am">Ma'am</option>
              <option value="none">None</option>
            </select>
          </div>

          <div class="settings-field">
            <label>Calendar Accounts</label>
            <textarea id="input-calendar-accounts" rows="2" placeholder="auto (or comma-separated emails)"></textarea>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-prefs">Save Preferences</button>
          </div>
        </section>

        <!-- System Info -->
        <section class="settings-section" id="section-sysinfo">
          <h3>System Info</h3>
          <div class="sysinfo-grid">
            <div class="sysinfo-row"><span class="sysinfo-label">Memory entries</span><span id="sysinfo-memory">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Tasks</span><span id="sysinfo-tasks">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Server port</span><span id="sysinfo-port">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Uptime</span><span id="sysinfo-uptime">--</span></div>
          </div>
        </section>

        <!-- Setup Navigation (first-time only) -->
        <div class="setup-nav" id="setup-nav" style="display:none">
          <button class="settings-btn primary" id="btn-setup-next">Next</button>
        </div>

      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Panel lifecycle
// ---------------------------------------------------------------------------

function createPanel(): HTMLElement {
  const container = document.createElement("div");
  container.id = "settings-container";
  container.innerHTML = buildPanelHTML();
  document.body.appendChild(container);
  return container;
}

function setDotStatus(id: string, status: "green" | "red" | "yellow" | "off") {
  const dot = document.getElementById(id);
  if (!dot) return;
  dot.className = "status-dot";
  if (status !== "off") dot.classList.add(`status-${status}`);
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

async function loadStatus() {
  try {
    const status = await apiGet<StatusResponse>("/api/settings/status");

    setDotStatus("status-server", "green");

    const serverDetail = document.getElementById("status-server-detail");
    if (serverDetail) serverDetail.textContent = `port ${status.server_port} | up ${formatUptime(status.uptime_seconds)}`;

    // API key status dots
    const hasMistralKey = !!status.env_keys_set.mistral;
    const hasCodestralKey = !!status.env_keys_set.codestral;
    setDotStatus("status-mistral", hasMistralKey ? "green" : "red");
    setDotStatus("status-codestral", hasCodestralKey ? "green" : "red");
    setDotStatus("status-edge-tts", status.env_keys_set.edge_tts ? "green" : "yellow");

    // System info
    const memEl = document.getElementById("sysinfo-memory");
    if (memEl) memEl.textContent = String(status.memory_count);
    const taskEl = document.getElementById("sysinfo-tasks");
    if (taskEl) taskEl.textContent = String(status.task_count);
    const portEl = document.getElementById("sysinfo-port");
    if (portEl) portEl.textContent = String(status.server_port);
    const upEl = document.getElementById("sysinfo-uptime");
    if (upEl) upEl.textContent = formatUptime(status.uptime_seconds);

    return status;
  } catch (e) {
    console.error("[settings] failed to load status:", e);
    setDotStatus("status-server", "red");
    return null;
  }
}

// ---------------------------------------------------------------------------
// Connection status — loaded from /api/connections (separate, richer probe)
// ---------------------------------------------------------------------------
interface ConnectionsResponse {
  connections: Record<string, string>;
  timestamp: number;
  cache_age_s?: number;
}

function connDot(val: string): "green" | "red" | "yellow" | "off" {
  if (val === "CONNECTED" || val === "ACTIVE") return "green";
  if (val === "FRONTEND" || val === "AUTH_REQUIRED" || val === "INSTALLED" || val === "RATE_LIMITED") return "yellow";
  return "red";
}

async function loadConnections() {
  try {
    const data = await apiGet<ConnectionsResponse>("/api/connections");
    const c = data.connections;

    // Apple services
    setDotStatus("status-calendar",           connDot(c.apple_calendar  ?? c.calendar ?? "DISCONNECTED"));
    setDotStatus("status-mail",               connDot(c.apple_mail      ?? c.mail     ?? "DISCONNECTED"));
    setDotStatus("status-notes",              connDot(c.apple_notes     ?? c.notes    ?? "DISCONNECTED"));

    // Dev tools
    setDotStatus("status-claude-cli",         connDot(c.cloudcode ?? "DISCONNECTED"));
    setDotStatus("status-speckit",            connDot(c.speckit   ?? "DISCONNECTED"));
    setDotStatus("status-antigravity",        connDot(c.antigravity ?? "DISCONNECTED"));
    setDotStatus("status-opencode",           connDot(c.opencode  ?? "DISCONNECTED"));
    setDotStatus("status-localai",            connDot(c.localai   ?? "DISCONNECTED"));
    setDotStatus("status-codex",              connDot(c.codex     ?? "DISCONNECTED"));
    setDotStatus("status-local-system",       connDot(c.local_system ?? "DISCONNECTED"));

    // Background service
    setDotStatus("status-background-service", connDot(c.background_service ?? "DISCONNECTED"));
  } catch (e) {
    console.warn("[settings] loadConnections failed:", e);
  }
}

async function loadPreferences() {
  try {
    const prefs = await apiGet<PreferencesResponse>("/api/settings/preferences");
    const nameEl = document.getElementById("input-user-name") as HTMLInputElement;
    const honEl = document.getElementById("input-honorific") as HTMLSelectElement;
    const calEl = document.getElementById("input-calendar-accounts") as HTMLTextAreaElement;
    if (nameEl) nameEl.value = prefs.user_name || "";
    if (honEl) honEl.value = prefs.honorific || "sir";
    if (calEl) calEl.value = prefs.calendar_accounts || "auto";
  } catch (e) {
    console.error("[settings] failed to load preferences:", e);
  }
}

function wireEvents() {
  // Close
  document.getElementById("settings-close")?.addEventListener("click", closeSettings);
  document.getElementById("settings-backdrop")?.addEventListener("click", closeSettings);

  // Save keys
  document.getElementById("btn-save-keys")?.addEventListener("click", async () => {
    const mistralKey = (document.getElementById("input-mistral-key") as HTMLInputElement).value.trim();
    const codestralKey = (document.getElementById("input-codestral-key") as HTMLInputElement).value.trim();
    const voiceSelect = (document.getElementById("input-voice-select") as HTMLSelectElement).value;

    if (mistralKey) {
      await apiPost("/api/settings/keys", { key_name: "MISTRAL_API_KEY", key_value: mistralKey });
    }
    if (codestralKey) {
      await apiPost("/api/settings/keys", { key_name: "CODESTRAL_API_KEY", key_value: codestralKey });
    }
    if (voiceSelect && voiceSelect !== "daniel-macos") {
      await apiPost("/api/settings/keys", { key_name: "EDGE_TTS_VOICE", key_value: voiceSelect });
    }
    await Promise.all([loadStatus(), loadConnections()]);
  });

  // Test Mistral
  document.getElementById("btn-test-mistral")?.addEventListener("click", async () => {
    setDotStatus("status-mistral", "yellow");
    const key = (document.getElementById("input-mistral-key") as HTMLInputElement).value.trim();
    try {
      const result = await apiPost<{ valid: boolean; error?: string }>("/api/settings/test-mistral", { key_value: key || undefined });
      setDotStatus("status-mistral", result.valid ? "green" : "red");
    } catch {
      setDotStatus("status-mistral", "red");
    }
  });

  // Test Codestral
  document.getElementById("btn-test-codestral")?.addEventListener("click", async () => {
    setDotStatus("status-codestral", "yellow");
    const key = (document.getElementById("input-codestral-key") as HTMLInputElement).value.trim();
    try {
      const result = await apiPost<{ valid: boolean; error?: string }>("/api/settings/test-codestral", { key_value: key || undefined });
      setDotStatus("status-codestral", result.valid ? "green" : "red");
    } catch {
      setDotStatus("status-codestral", "red");
    }
  });

  // Save preferences
  document.getElementById("btn-save-prefs")?.addEventListener("click", async () => {
    const user_name = (document.getElementById("input-user-name") as HTMLInputElement).value.trim();
    const honorific = (document.getElementById("input-honorific") as HTMLSelectElement).value;
    const calendar_accounts = (document.getElementById("input-calendar-accounts") as HTMLTextAreaElement).value.trim();
    await apiPost("/api/settings/preferences", { user_name, honorific, calendar_accounts });
    await Promise.all([loadStatus(), loadConnections()]);
  });

  // Setup next button
  document.getElementById("btn-setup-next")?.addEventListener("click", advanceSetup);
}

// ---------------------------------------------------------------------------
// First-time setup wizard
// ---------------------------------------------------------------------------

function enterSetupMode() {
  isFirstTimeSetup = true;
  setupStep = 0;

  const welcome = document.getElementById("settings-welcome");
  if (welcome) welcome.style.display = "block";

  const nav = document.getElementById("setup-nav");
  if (nav) nav.style.display = "flex";

  // Hide sections except API keys
  showSetupStep(0);
}

function showSetupStep(step: number) {
  const sections = ["section-api-keys", "section-status", "section-preferences", "section-sysinfo"];
  sections.forEach((id, i) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (step === 0 && i === 0) el.style.display = "";
    else if (step === 1 && i === 2) el.style.display = "";
    else if (step === 2) el.style.display = "";
    else el.style.display = "none";
  });

  const nextBtn = document.getElementById("btn-setup-next");
  if (nextBtn) {
    if (step === 0) nextBtn.textContent = "Next: Set Your Name";
    else if (step === 1) nextBtn.textContent = "Finish Setup";
    else nextBtn.style.display = "none";
  }
}

async function advanceSetup() {
  setupStep++;
  if (setupStep >= 2) {
    // Done — save everything and close
    isFirstTimeSetup = false;
    const welcome = document.getElementById("settings-welcome");
    if (welcome) welcome.style.display = "none";
    const nav = document.getElementById("setup-nav");
    if (nav) nav.style.display = "none";

    // Show all sections
    ["section-api-keys", "section-status", "section-preferences", "section-sysinfo"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "";
    });

    closeSettings();
    return;
  }
  showSetupStep(setupStep);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function openSettings() {
  if (isOpen) return;
  isOpen = true;

  if (!panelEl) {
    panelEl = createPanel();
    wireEvents();
  }

  panelEl.style.display = "block";

  // Trigger animation
  requestAnimationFrame(() => {
    panelEl!.classList.add("open");
  });
  document.dispatchEvent(new CustomEvent("jarvis:settings-visibility", { detail: { open: true } }));

  // Load data — run status + connections in parallel
  const [status] = await Promise.all([loadStatus(), loadConnections(), loadPreferences()]);

  // Check for first-time setup
  if (status && !status.env_keys_set.mistral) {
    enterSetupMode();
  }
}

export function closeSettings() {
  if (!panelEl || !isOpen) return;
  isOpen = false;
  document.dispatchEvent(new CustomEvent("jarvis:settings-visibility", { detail: { open: false } }));
  panelEl.classList.remove("open");
  setTimeout(() => {
    if (panelEl) panelEl.style.display = "none";
  }, 300);
}

export function isSettingsOpen(): boolean {
  return isOpen;
}

/**
 * Check if first-time setup is needed and auto-open.
 */
export async function checkFirstTimeSetup(): Promise<boolean> {
  try {
    const status = await apiGet<StatusResponse>("/api/settings/status");
    if (!status.env_keys_set.mistral) {
      openSettings();
      return true;
    }
  } catch {
    // Server not ready yet, skip
  }
  return false;
}
