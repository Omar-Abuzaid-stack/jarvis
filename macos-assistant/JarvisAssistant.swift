import AVFoundation
import Foundation
import Speech
import AppKit

enum AssistantMode: String {
    case mode1_waiting     // http://localhost:8340/ is CLOSED: MIC ARMED
    case mode2_session     // Active conversation session (within 60s)
    case mode3_standby     // http://localhost:8340/ is OPEN: MIC DISARMED (Standby)
    case mode_error        // Failure state
}

final class JarvisAssistant: NSObject {
    private struct QueuedAudio {
        let data: Data
        let attempt: Int
    }

    private let serverURL: URL
    private let targetURL = "http://localhost:8340/" // Deterministic canonical URL
    private let sessionID = UUID().uuidString
    private let wakeWords = ["hey jarvis", "ok jarvis", "okay jarvis", "jarvis"]
    private let wakeVocabulary = ["Hey Jarvis", "Jarvis", "Mr Omar", "At your services Mr Omar"]
    private let wakeDebounce: TimeInterval = 4
    private let activeSessionWindow: TimeInterval = 60
    private let tabCheckInterval: TimeInterval = 4 // More frequent (4s) for faster re-arm
    private let speechLocaleIdentifier = Locale.preferredLanguages.first(where: { $0.lowercased().hasPrefix("en") }) ?? "en-US"

    private var speechRecognizer: SFSpeechRecognizer?
    private var audioEngine: AVAudioEngine?
    private var inputNode: AVAudioInputNode?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    
    private var statusItem: NSStatusItem?
    private var currentMode: AssistantMode = .mode1_waiting {
        didSet {
            if oldValue != currentMode {
                log("[STATE] \(oldValue) -> \(currentMode)")
                updateMenuBarStatus()
            }
        }
    }
    
    private var audioQueue: [QueuedAudio] = []
    private var audioPlaybackPlayer: AVAudioPlayer?
    private var audioPlaybackPath: String?
    private var lastWakeAt = Date.distantPast
    private var isPlayingAudio = false
    private var lastSpeechAt = Date.distantPast
    private var sessionResetTask: DispatchWorkItem?
    private var tabMonitorTimer: Timer?

    init(serverURL: URL) {
        self.serverURL = serverURL
        super.init()
    }

    private func createStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem?.button {
            button.title = "J"
            button.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .bold)
        }
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "JARVIS Native Assistant", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Check Status / Self-Test", action: #selector(doStatusCheck), keyEquivalent: "i"))
        menu.addItem(NSMenuItem(title: "Manual Re-Arm", action: #selector(doRestart), keyEquivalent: "r"))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        statusItem?.menu = menu
        updateMenuBarStatus()
    }

    private func updateMenuBarStatus() {
        DispatchQueue.main.async { [weak self] in
            guard let button = self?.statusItem?.button else { return }
            switch self?.currentMode {
            case .mode1_waiting:
                button.title = "Waiting for Jarvis ○"
                button.contentTintColor = NSColor.secondaryLabelColor
            case .mode2_session:
                button.title = "Jarvis Active ●"
                button.contentTintColor = NSColor.systemOrange
            case .mode3_standby:
                button.title = "Jarvis Page Open"
                button.contentTintColor = NSColor.systemGreen
            case .mode_error:
                button.title = "Error / Not Armed !"
                button.contentTintColor = NSColor.systemRed
            default: break
            }
        }
    }

    @objc private func doStatusCheck() {
        let tabOpen = checkJarvisTabOpen()
        let alert = NSAlert()
        alert.messageText = "JARVIS Status Audit"
        alert.informativeText = "Current Mode: \(currentMode.rawValue)\nJarvis Tab: \(tabOpen ? "OPEN" : "CLOSED")\nMic Armed: \(audioEngine?.isRunning ?? false)\nServer: \(serverURL.absoluteString)"
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    @objc private func doRestart() {
        log("[STATE] manual re-arm triggered. entering mode1_waiting")
        currentMode = .mode1_waiting
        Task { await startMicLoop() }
    }

    func run() {
        log("[BOOT] native assistant starting server=\(serverURL.absoluteString)")
        createStatusItem()
        Task { await bootstrap() }
        startTabMonitoring()
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        app.run()
    }

    private func bootstrap() async {
        log("[BOOT] checking microphone permissions")
        let permissionsOK = await ensurePermissions(interactive: true)
        guard permissionsOK else {
            log("[ERROR] mic permissions DENIED by macOS TCC. No visibility possible."); currentMode = .mode_error; return
        }
        log("[BOOT] permissions granted")
        if checkJarvisTabOpen() {
            log("[STATE] JARVIS tab already open at \(targetURL). Initializing in standby.")
            currentMode = .mode3_standby
        } else {
            log("[STATE] No JARVIS tab found. Initializing in waiting mode.")
            currentMode = .mode1_waiting
            await startMicLoop()
        }
    }

    private func ensurePermissions(interactive: Bool) async -> Bool {
        let speechStatus = SFSpeechRecognizer.authorizationStatus()
        let micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        if interactive {
            if speechStatus == .notDetermined { await SFSpeechRecognizer.requestAuthorization { _ in } }
            if micStatus == .notDetermined { await AVCaptureDevice.requestAccess(for: .audio) { _ in } }
        }
        return SFSpeechRecognizer.authorizationStatus() == .authorized && 
               AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
    }

    // -- TAB TRACKING ------------------------------------------------------

    private func startTabMonitoring() {
        tabMonitorTimer = Timer.scheduledTimer(withTimeInterval: tabCheckInterval, repeats: true) { [weak self] _ in
            self?.auditTabExistence()
        }
    }

    private func auditTabExistence() {
        let isOpen = checkJarvisTabOpen()
        
        // 1. If tab is open, do NOT disarm. The user wants global wake word active ALWAYS.
        if isOpen && currentMode == .mode1_waiting {
            log("[STATE] jarvis_tab_open=true -> staying in waiting mode (armed)")
            currentMode = .mode3_standby 
            // Do NOT stopMic() here. We want 'Jarvis' to still work for focusing/refreshing.
            updateMenuBarStatus()
        }
        
        // 2. RE-ARM: If tab was open but is now closed, re-arm automatically
        if !isOpen && currentMode == .mode3_standby {
            log("[STATE] no jarvis tab remains at \(targetURL) -> rearming wake listener")
            currentMode = .mode1_waiting
            Task { await startMicLoop() }
        }
    }

    private func checkJarvisTabOpen() -> Bool {
        let script = """
        set found to false
        set targetURL to "\(targetURL)"
        set browsers to {"Comet", "Google Chrome", "Safari", "Firefox", "Arc", "Brave Browser", "Microsoft Edge"}
        
        tell application "System Events"
            repeat with bName in browsers
                if (exists process bName) then
                    try
                        tell application bName
                            repeat with w in windows
                                try
                                    repeat with t in tabs of w
                                        if URL of t contains targetURL then
                                            set found to true
                                            exit repeat
                                        end if
                                    end repeat
                                end try
                                if found then exit repeat
                            end repeat
                        end tell
                    end try
                end if
                if found then exit repeat
            end repeat
        end tell
        return found
        """
        
        let process = Process()
        process.launchPath = "/usr/bin/osascript"
        process.arguments = ["-e", script]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.launch()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        if let res = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) {
            return res == "true"
        }
        return false
    }

    // -- MIC AND RECOGNITION ------------------------------------------------

    private func stopMic() {
        log("[MIC] stop acquisition requested")
        recognitionTask?.cancel(); recognitionTask = nil
        recognitionRequest?.endAudio(); recognitionRequest = nil
        if let node = inputNode { node.removeTap(onBus: 0); inputNode = nil }
        audioEngine?.stop(); audioEngine = nil
    }

    private func startMicLoop() async {
        stopMic()
        guard !isPlayingAudio && currentMode != .mode3_standby else { return }
        
        let recognizer = SFSpeechRecognizer(locale: Locale(identifier: speechLocaleIdentifier))
        guard let recognizer = recognizer, recognizer.isAvailable else {
            log("[ERROR] SFSpeechRecognizer unavailable"); currentMode = .mode_error; return
        }
        
        log("[MIC] start acquisition attempt owner=native mode=\(currentMode)")
        do {
            let engine = AVAudioEngine()
            audioEngine = engine
            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            request.contextualStrings = wakeVocabulary
            if recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
            recognitionRequest = request
            
            let node = engine.inputNode
            inputNode = node
            let format = node.inputFormat(forBus: 0)
            node.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
                self?.recognitionRequest?.append(buffer)
            }
            
            engine.prepare()
            try engine.start()
            log("[BOOT] mic successfully armed. yellow dot should be visible. state=\(currentMode.rawValue)")
            updateMenuBarStatus()
        } catch {
            log("[ERROR] AVAudioEngine failed to start: \(error.localizedDescription)")
            currentMode = .mode_error
            scheduleRestart(delay: 15.0)
            return
        }
        
        recognitionTask = recognizer.recognitionTask(with: recognitionRequest!) { [weak self] result, error in
            guard let self = self else { return }
            if let result = result {
                let text = result.bestTranscription.formattedString
                if result.isFinal { self.handleFinalTranscript(text) } else { self.handlePartialTranscript(text) }
            }
            if let error = error {
                let nsError = error as NSError
                if nsError.code != 301 && nsError.code != 216 && nsError.code != 401 {
                    self.log("[ERROR] recognition task failed: \(error.localizedDescription)")
                    self.scheduleRestart(delay: 5.0)
                }
            }
        }
    }

    private func scheduleRestart(delay: TimeInterval) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard self?.currentMode != .mode3_standby else { return }
            Task { await self?.startMicLoop() }
        }
    }

    private func handlePartialTranscript(_ text: String) {
        if currentMode == .mode1_waiting {
            if let _ = extractWakeRemainder(text) { Task { await triggerWake() } }
        }
    }

    private func handleFinalTranscript(_ text: String) {
        log("heard text=\(text.replacingOccurrences(of: "\n", with: " "))")
        if currentMode == .mode1_waiting {
            if let remainder = extractWakeRemainder(text) {
                Task { await triggerWake(); if !remainder.isEmpty { await sendTurn(remainder) } }
                return
            }
        }
        if currentMode == .mode2_session { Task { await sendTurn(text) } }
    }

    private func extractWakeRemainder(_ rawText: String) -> String? {
        let normalized = rawText.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        for wake in wakeWords {
            if normalized.contains(wake) {
                if let range = normalized.range(of: wake) {
                    let remainder = normalized[range.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
                    return remainder
                }
            }
        }
        return nil
    }

    private func triggerWake() async {
        let now = Date()
        guard now.timeIntervalSince(lastWakeAt) >= wakeDebounce else { return }
        lastWakeAt = now
        log("[WAKE] accepted source=native phrase=jarvis")
        stopMic()
        do {
            let res = try await postJSON(path: "/api/wake", body: ["source": "mac"])
            if res["status"] as? String == "accepted" {
                currentMode = .mode2_session
                resetSessionTimer()
            }
            await startMicLoop()
        } catch {
            log("[ERROR] wake signal failed: \(error.localizedDescription)")
            await startMicLoop()
        }
    }

    private func sendTurn(_ text: String) async {
        guard !text.isEmpty else { return }
        resetSessionTimer()
        _ = try? await postJSON(path: "/api/assistant/signal", body: ["state": "thinking"])
        do {
            log("[ACTIVE] turn_sent text=\(text)")
            let payload = try await postJSON(path: "/api/assistant/turn", body: ["text": text, "session_id": sessionID, "source": "mac"])
            if let audio = payload["audio"] as? String {
                _ = try? await postJSON(path: "/api/assistant/signal", body: ["state": "speaking"])
                enqueueAudio(base64: audio)
            } else {
                _ = try? await postJSON(path: "/api/assistant/signal", body: ["state": "idle"])
            }
        } catch {
            log("[ERROR] turn failed: \(error.localizedDescription)")
            _ = try? await postJSON(path: "/api/assistant/signal", body: ["state": "idle"])
        }
    }

    private func resetSessionTimer() {
        sessionResetTask?.cancel()
        let task = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            self.log("[SESSION] timed out after \(self.activeSessionWindow)s silence")
            self.currentMode = checkJarvisTabOpen() ? .mode3_standby : .mode1_waiting
            Task { _ = try? await self.postJSON(path: "/api/assistant/signal", body: ["state": "idle"]) }
            Task { await self.startMicLoop() }
        }
        sessionResetTask = task
        DispatchQueue.main.asyncAfter(deadline: .now() + activeSessionWindow, execute: task)
    }

    private func postJSON(path: String, body: [String: Any]) async throws -> [String: Any] {
        var request = URLRequest(url: serverURL.appendingPathComponent(path), timeoutInterval: 120.0)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw NSError(domain: "JarvisAssistant", code: 1, userInfo: [NSLocalizedDescriptionKey: "HTTP Error"])
        }
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    // -- AUDIO PLAYBACK -----------------------------------------------------

    private func enqueueAudio(base64: String) {
        guard let data = Data(base64Encoded: base64), !data.isEmpty else { return }
        audioQueue.append(QueuedAudio(data: data, attempt: 0))
        playNextIfNeeded()
    }

    private func playNextIfNeeded() {
        guard audioPlaybackPlayer == nil, !audioQueue.isEmpty else { return }
        let item = audioQueue.removeFirst()
        isPlayingAudio = true
        stopMic()
        do {
            let path = try writeAudioFile(item.data)
            audioPlaybackPath = path
            let player = try AVAudioPlayer(contentsOf: URL(fileURLWithPath: path))
            player.delegate = self
            player.prepareToPlay()
            player.play()
            audioPlaybackPlayer = player
        } catch {
            log("[ERROR] playback failed: \(error.localizedDescription)")
            handlePlaybackFinished()
        }
    }

    private func handlePlaybackFinished() {
        audioPlaybackPlayer = nil
        if let path = audioPlaybackPath { try? FileManager.default.removeItem(atPath: path); audioPlaybackPath = nil }
        if audioQueue.isEmpty {
            isPlayingAudio = false
            Task { _ = try? await self.postJSON(path: "/api/assistant/signal", body: ["state": "idle"]) }
            Task { await startMicLoop() }
        } else {
            playNextIfNeeded()
        }
    }

    private func writeAudioFile(_ data: Data) throws -> String {
        let path = NSTemporaryDirectory() + "jarvis-\(UUID().uuidString).mp3"
        try data.write(to: URL(fileURLWithPath: path))
        return path
    }

    private func log(_ message: String) {
        let timestamp = ISO8601DateFormatter().string(from: Date())
        print("[\(timestamp)] [jarvis.helper] \(message)")
        fflush(stdout)
    }
}

extension JarvisAssistant: AVAudioPlayerDelegate {
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        DispatchQueue.main.async { self.handlePlaybackFinished() }
    }
}

let baseURLString = CommandLine.arguments.dropFirst().first ?? ProcessInfo.processInfo.environment["JARVIS_SERVER_URL"] ?? "http://127.0.0.1:8340"
guard let serverURL = URL(string: baseURLString) else { exit(1) }
JarvisAssistant(serverURL: serverURL).run()
