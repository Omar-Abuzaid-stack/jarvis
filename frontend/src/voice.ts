/**
 * Voice input placeholder and audio output for JARVIS.
 * UPDATED: Browser-side microphone ownership REMOVED.
 * Native/background process now owns the mic lifecycle exclusively.
 */

export interface VoiceInput {
  start(): void;
  stop(): void;
  pause(): void;
  resume(): void;
  setActive(active: boolean): void;
  isActive(): boolean;
}

export function createVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string, reason?: any) => void,
): VoiceInput {
  const SpeechRecognition = (window as any).webkitSpeechRecognition || (window as any).SpeechRecognition;
  if (!SpeechRecognition) {
    return {
      start() { onError("Speech recognition not supported."); },
      stop() {}, pause() {}, resume() {}, setActive() {},
      isActive: () => false,
    };
  }

  const recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  let active = false;
  let silenceTimer: any = null;
  let currentInterim = "";

  const submitNow = () => {
    if (currentInterim.trim()) {
      console.log("[MIC] Force-submitting after silence:", currentInterim);
      onTranscript(currentInterim.trim());
      currentInterim = "";
    }
  };

  recognition.onresult = (event: any) => {
    clearTimeout(silenceTimer);
    
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        onTranscript(transcript.trim());
        currentInterim = "";
      } else {
        currentInterim = transcript;
        // If we have interim results but no final result for 1.2s, force submit.
        silenceTimer = setTimeout(submitNow, 1200);
      }
    }
  };

  recognition.onerror = (event: any) => {
    if (event.error === "no-speech" || event.error === "aborted") return;
    onError(event.error);
  };

  recognition.onend = () => {
    if (active) {
      try { recognition.start(); } catch {}
    }
  };

  return {
    start() {
      active = true;
      try { recognition.start(); } catch {}
    },
    stop() {
      active = false;
      clearTimeout(silenceTimer);
      try { recognition.stop(); } catch {}
    },
    pause() { try { recognition.stop(); } catch {} },
    resume() { 
      if (active) {
        try { recognition.start(); } catch {}
      }
    },
    setActive(val: boolean) { active = val; },
    isActive: () => active,
  };
}

// ---------------------------------------------------------------------------
// Audio Player (Keep for output only)
// ---------------------------------------------------------------------------

export interface AudioPlayer {
  enqueue(base64: string): Promise<void>;
  stop(): void;
  getAnalyser(): AnalyserNode;
  onFinished(cb: () => void): void;
}

export function createAudioPlayer(): AudioPlayer {
  const audioCtx = new AudioContext();
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  const gainNode = audioCtx.createGain();
  gainNode.gain.value = 1.3;
  analyser.connect(gainNode);
  gainNode.connect(audioCtx.destination);

  const audioEl = new Audio();
  audioEl.volume = 1.0;
  audioEl.playbackRate = 1.05;
  const mediaSource = audioCtx.createMediaElementSource(audioEl);
  mediaSource.connect(analyser);

  const queue: { url: string }[] = [];
  let isPlaying = false;
  let currentUrl = "";
  let finishedCallback: (() => void) | null = null;

  async function playNext() {
    if (queue.length === 0) {
      isPlaying = false;
      finishedCallback?.();
      return;
    }
    isPlaying = true;
    const item = queue.shift()!;
    if (currentUrl) URL.revokeObjectURL(currentUrl);
    currentUrl = item.url;
    audioEl.src = currentUrl;
    audioEl.onended = () => { void playNext(); };
    audioEl.onerror = () => { void playNext(); };
    try { await audioEl.play(); } catch { void playNext(); }
  }

  return {
    async enqueue(base64: string) {
      if (audioCtx.state === "suspended") await audioCtx.resume();
      try {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        const blob = new Blob([bytes], { type: "audio/mpeg" });
        const url = URL.createObjectURL(blob);
        queue.push({ url });
        if (!isPlaying) void playNext();
      } catch (err) { console.error("[audio] enqueue error:", err); }
    },
    stop() {
      queue.length = 0;
      audioEl.pause();
      audioEl.removeAttribute("src");
      audioEl.load();
      isPlaying = false;
    },
    getAnalyser() { return analyser; },
    onFinished(cb: () => void) { finishedCallback = cb; },
  };
}
