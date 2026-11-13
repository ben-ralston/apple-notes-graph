import EventKit
import Foundation

let NOTES_DB = NSString(
    string: "~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
).expandingTildeInPath

func log(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

func probeOpenAndRead(label: String) -> Int32 {
    log("probe[\(label)]: opening \(NOTES_DB)")
    guard let handle = FileHandle(forReadingAtPath: NOTES_DB) else {
        log("probe[\(label)]: FAIL (FileHandle nil — TCC denied or file missing)")
        return 1
    }
    do {
        let data = try handle.read(upToCount: 16) ?? Data()
        try handle.close()
        if data.count == 16 {
            log("probe[\(label)]: OK (read 16 bytes)")
            return 0
        }
        log("probe[\(label)]: FAIL (only \(data.count) bytes)")
        return 1
    } catch {
        log("probe[\(label)]: FAIL (\(error))")
        return 1
    }
}

func childEnvironment() -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    env["HOME"] = home
    if env["PATH"] == nil {
        env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    }
    env.removeValue(forKey: "CLAUDECODE")
    env.removeValue(forKey: "CLAUDE_CODE")
    return env
}

func runChildAndWait(executable: String, args: [String]) -> Int32 {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = args
    process.environment = childEnvironment()
    do {
        try process.run()
        process.waitUntilExit()
        return process.terminationStatus
    } catch {
        log("Failed to launch \(executable): \(error)")
        return 1
    }
}

// Python entry resolution priority:
// 1. NOTES_GRAPH_PYTHON_ENTRY env var (used during spike before install)
// 2. Contents/Resources/python-entry.txt inside the .app (written by mcp-app-install)
// 3. ~/Library/Application Support/notes-graph/python-entry.txt (manual override)
func resolvePythonEntry() -> String? {
    if let env = ProcessInfo.processInfo.environment["NOTES_GRAPH_PYTHON_ENTRY"] {
        return env
    }
    if let url = Bundle.main.url(forResource: "python-entry", withExtension: "txt"),
       let contents = try? String(contentsOf: url, encoding: .utf8) {
        let trimmed = contents.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed }
    }
    let fallback = "\(FileManager.default.homeDirectoryForCurrentUser.path)/Library/Application Support/notes-graph/python-entry.txt"
    if let contents = try? String(contentsOfFile: fallback, encoding: .utf8) {
        let trimmed = contents.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed }
    }
    return nil
}

func resolvedVenvPython() -> String? {
    guard let entry = resolvePythonEntry() else { return nil }
    let entryURL = URL(fileURLWithPath: entry)
    let pythonURL = entryURL.deletingLastPathComponent().appendingPathComponent("python")
    return FileManager.default.fileExists(atPath: pythonURL.path) ? pythonURL.path : nil
}

func bridgeStdio() -> Never {
    guard let entry = resolvePythonEntry() else {
        log("bridge-stdio: FAIL — no python entry found (set NOTES_GRAPH_PYTHON_ENTRY or run ./run mcp-app-install)")
        exit(1)
    }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: entry)
    process.arguments = []
    process.standardInput = FileHandle.standardInput
    process.standardOutput = FileHandle.standardOutput
    process.standardError = FileHandle.standardError
    process.environment = childEnvironment()

    let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
    let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
    sigint.setEventHandler { process.interrupt() }
    sigterm.setEventHandler { process.terminate() }
    sigint.resume()
    sigterm.resume()
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)

    do {
        try process.run()
        process.waitUntilExit()
        exit(process.terminationStatus)
    } catch {
        log("bridge-stdio: failed to launch \(entry): \(error)")
        exit(1)
    }
}

// Reminders access for this app's identity (issue #171). Under launchd, the
// remote server's python child is checked against this bundle, so the grant
// must belong to AppleNotesMCP.app, not to a terminal.
func remindersStatusWord() -> String {
    switch EKEventStore.authorizationStatus(for: .reminder).rawValue {
    case 0: return "not-determined"
    case 1: return "restricted"
    case 2: return "denied"
    case 3: return "granted"
    case 4: return "write-only"
    default: return "unknown"
    }
}

func reportRemindersStatus() -> Int32 {
    let word = remindersStatusWord()
    FileHandle.standardOutput.write(Data((word + "\n").utf8))
    return word == "granted" ? 0 : 1
}

// Asks once (the system prompt names "Apple Notes MCP"); the owner runs this while
// the Mac is unlocked. Launch it with `open` so the app is its own
// responsible process.
func probeReminders() -> Int32 {
    if remindersStatusWord() == "not-determined" {
        let store = EKEventStore()
        var answered = false
        let done: (Bool, Error?) -> Void = { _, _ in
            DispatchQueue.main.async { answered = true }
        }
        if #available(macOS 14.0, *) {
            store.requestFullAccessToReminders(completion: done)
        } else {
            store.requestAccess(to: .reminder, completion: done)
        }
        let deadline = Date().addingTimeInterval(120)
        while !answered && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        }
        if !answered {
            log("probe[reminders]: no answer within 120s")
        }
    }
    return reportRemindersStatus()
}

let args = CommandLine.arguments

guard args.count >= 2 else {
    log("Usage: AppleNotesMCPRunner <--probe-self|--probe-python3|--probe-uv-venv|--bridge-stdio|--reminders-status|--probe-reminders>")
    exit(2)
}

let probeScript = "import sys; f = open('\(NOTES_DB)', 'rb'); d = f.read(16); f.close(); sys.exit(0 if len(d) == 16 else 1)"

switch args[1] {
case "--probe-self":
    exit(probeOpenAndRead(label: "self"))

case "--probe-python3":
    log("probe[python3]: spawning /usr/bin/python3")
    exit(runChildAndWait(executable: "/usr/bin/python3", args: ["-c", probeScript]))

case "--probe-uv-venv":
    guard let python = resolvedVenvPython() else {
        log("probe[uv-venv]: FAIL — could not resolve venv python (run ./run mcp-app-install or set NOTES_GRAPH_PYTHON_ENTRY)")
        exit(1)
    }
    log("probe[uv-venv]: spawning \(python)")
    exit(runChildAndWait(executable: python, args: ["-c", probeScript]))

case "--bridge-stdio":
    bridgeStdio()

case "--reminders-status":
    exit(reportRemindersStatus())

case "--probe-reminders":
    exit(probeReminders())

default:
    log("Unknown argument: \(args[1])")
    exit(2)
}
