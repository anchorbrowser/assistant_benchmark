import AppKit
import ApplicationServices
import Foundation

enum BridgeError: Error, CustomStringConvertible {
    case usage
    case messagesNotRunning
    case accessibilityDenied
    case composerNotFound
    case sendNotConfirmed
    case pasteMismatch(expected: String, observed: String)
    case ax(String, AXError)

    var description: String {
        switch self {
        case .usage:
            return "usage: imessage_bridge send|read [display-name]|transcript"
        case .messagesNotRunning:
            return "Messages.app is not running"
        case .accessibilityDenied:
            return "Accessibility access denied for this executable"
        case .composerNotFound:
            return "Messages composer (messageBodyField) not found"
        case .sendNotConfirmed:
            return "text stayed in the composer; message was not sent"
        case let .pasteMismatch(expected, observed):
            return "composer never took the text. expected ~\(expected.debugDescription), "
                + "observed \(observed.debugDescription)"
        case let .ax(operation, error):
            return "\(operation) failed with AX error \(error.rawValue)"
        }
    }
}

func attribute(_ element: AXUIElement, _ name: CFString) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name, &value) == .success else {
        return nil
    }
    return value
}

func stringAttribute(_ element: AXUIElement, _ name: CFString) -> String? {
    attribute(element, name) as? String
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    (attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement]) ?? []
}

func windows(_ application: AXUIElement) -> [AXUIElement] {
    (attribute(application, kAXWindowsAttribute as CFString) as? [AXUIElement]) ?? []
}

func roots(_ application: AXUIElement) -> [AXUIElement] {
    var found: [AXUIElement] = []
    var seen = Set<CFHashCode>()
    for name in [
        kAXFocusedWindowAttribute, kAXMainWindowAttribute,
    ] as [String] {
        if let value = attribute(application, name as CFString) {
            let window = (value as! AXUIElement)
            if seen.insert(CFHash(window)).inserted { found.append(window) }
        }
    }
    for window in windows(application) where seen.insert(CFHash(window)).inserted {
        found.append(window)
    }
    return found.isEmpty ? children(application) : found
}

func pid(of element: AXUIElement) -> pid_t? {
    var value: pid_t = 0
    guard AXUIElementGetPid(element, &value) == .success else { return nil }
    return value
}

func focusedElement() -> AXUIElement? {
    guard let value = attribute(
        AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute as CFString
    ) else { return nil }
    return (value as! AXUIElement)
}

// The system-wide focused element belongs to whichever app has keyboard focus,
// which is the benchmark's terminal unless Messages just came forward. Only
// trust it when it really is inside the Messages process.
func focusedElement(in application: NSRunningApplication) -> AXUIElement? {
    guard let element = focusedElement(),
          pid(of: element) == application.processIdentifier else { return nil }
    return element
}

func searchRoots(_ application: NSRunningApplication, _ axApp: AXUIElement) -> [AXUIElement] {
    if let window = focusedElement(in: application).flatMap(containingWindow) {
        return [window]
    }
    return roots(axApp)
}

func containingWindow(_ element: AXUIElement) -> AXUIElement? {
    var current: AXUIElement? = element
    for _ in 0..<20 {
        guard let item = current else { return nil }
        if stringAttribute(item, kAXRoleAttribute as CFString) == (kAXWindowRole as String) {
            return item
        }
        guard let parent = attribute(item, kAXParentAttribute as CFString) else {
            return nil
        }
        current = (parent as! AXUIElement)
    }
    return nil
}

func walk(_ element: AXUIElement, depth: Int = 0, visit: (AXUIElement) -> Bool) {
    guard depth < 20, !visit(element) else { return }
    for child in children(element) {
        walk(child, depth: depth + 1, visit: visit)
    }
}

// Activation is deliberately optional. A fullscreen app on another space can
// refuse to yield the front, and the accessibility path does not need it.
func messagesApplication(activating: Bool = false) throws -> (NSRunningApplication, AXUIElement) {
    guard AXIsProcessTrusted() else {
        throw BridgeError.accessibilityDenied
    }
    guard let app = NSRunningApplication
        .runningApplications(withBundleIdentifier: "com.apple.MobileSMS")
        .first else {
        throw BridgeError.messagesNotRunning
    }
    if activating {
        _ = app.activate()
        for _ in 0..<20 where !app.isActive {
            Thread.sleep(forTimeInterval: 0.05)
        }
        Thread.sleep(forTimeInterval: 0.2)
    }
    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    // SwiftUI/scene-based apps may withhold their window tree until enhanced
    // accessibility is enabled. System Events does this implicitly.
    AXUIElementSetAttributeValue(
        axApp, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue
    )
    return (app, axApp)
}

// Key events posted to the process are routed to its key window, which only
// exists while Messages is frontmost, so sending has to take the front. Only
// sending does: polling reads the accessibility tree and stays out of the way.
func send(_ text: String) throws {
    let (app, axApp) = try messagesApplication(activating: true)
    var composer = focusedElement(in: app)
    if stringAttribute(composer ?? axApp, kAXIdentifierAttribute as CFString) != "messageBodyField" {
        composer = nil
        for window in searchRoots(app, axApp) where composer == nil {
            walk(window) { element in
                if stringAttribute(element, kAXIdentifierAttribute as CFString) == "messageBodyField" {
                    composer = element
                    return true
                }
                return false
            }
        }
    }
    guard let composer else { throw BridgeError.composerNotFound }

    // Setting AXFocused alone does not always make the field first responder,
    // and key events then go nowhere. Pressing it first is the equivalent of
    // clicking into the box, which does.
    AXUIElementPerformAction(composer, kAXPressAction as CFString)
    let focusError = AXUIElementSetAttributeValue(
        composer, kAXFocusedAttribute as CFString, kCFBooleanTrue
    )
    guard focusError == .success else {
        throw BridgeError.ax("focus composer", focusError)
    }
    Thread.sleep(forTimeInterval: 0.2)
    AXUIElementSetAttributeValue(composer, kAXValueAttribute as CFString, "" as CFString)

    // Writing AXValue puts text on screen but leaves Messages believing the
    // composer is empty: it never publishes a send control and Return is
    // ignored. Only real key events make the message sendable. Posting them to
    // the process rather than the window server means a fullscreen app on
    // another space cannot swallow them, and nothing steals focus.
    let pasteboard = NSPasteboard.general
    let savedClipboard = pasteboard.string(forType: .string)
    defer {
        if let savedClipboard {
            pasteboard.clearContents()
            pasteboard.setString(savedClipboard, forType: .string)
        }
    }
    pasteboard.clearContents()
    pasteboard.setString(text, forType: .string)

    let pid = app.processIdentifier
    // Select-all first: clearing AXValue does not always empty the field, and a
    // surviving character would silently corrupt the head of the prompt.
    postKey(pid, virtualKey: 0x00, flags: .maskCommand)  // cmd-A
    postKey(pid, virtualKey: 0x09, flags: .maskCommand)  // cmd-V
    if !settled(composer, contains: text) {
        // A space switch or a fullscreen app can delay the front changing hands.
        _ = try? messagesApplication(activating: true)
        AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        postKey(pid, virtualKey: 0x00, flags: .maskCommand)
        postKey(pid, virtualKey: 0x09, flags: .maskCommand)
        guard settled(composer, contains: text) else {
            let observed = composerValue(composer)
            throw BridgeError.pasteMismatch(
                expected: String(text.prefix(40)), observed: String(observed.prefix(80))
            )
        }
    }

    postKey(pid, virtualKey: 0x24)  // return
    // An empty composer is the only trustworthy evidence the bubble left.
    guard drained(composer) else {
        throw BridgeError.sendNotConfirmed
    }
}

func postKey(_ pid: pid_t, virtualKey: CGKeyCode, flags: CGEventFlags = []) {
    guard let source = CGEventSource(stateID: .hidSystemState),
          let down = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: true),
          let up = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: false)
    else { return }
    down.flags = flags
    up.flags = flags
    down.postToPid(pid)
    up.postToPid(pid)
    Thread.sleep(forTimeInterval: 0.15)
}

func settled(_ composer: AXUIElement, contains text: String) -> Bool {
    // Messages normalises whitespace in long pastes, so compare on a stable
    // head fragment rather than demanding an exact match.
    let needle = String(text.prefix(24))
    for _ in 0..<30 {
        if composerValue(composer).contains(needle) { return true }
        Thread.sleep(forTimeInterval: 0.15)
    }
    return false
}

func composerValue(_ composer: AXUIElement) -> String {
    stringAttribute(composer, kAXValueAttribute as CFString) ?? ""
}

func drained(_ composer: AXUIElement) -> Bool {
    for _ in 0..<20 {
        if composerValue(composer).isEmpty { return true }
        Thread.sleep(forTimeInterval: 0.15)
    }
    return false
}

func awaitSendControl(_ axApp: AXUIElement) -> AXUIElement? {
    var sendItem: AXUIElement?
    for attempt in 0..<20 where sendItem == nil {
        if attempt > 0 { Thread.sleep(forTimeInterval: 0.15) }
        for root in children(axApp) where sendItem == nil {
            walk(root) { element in
                if stringAttribute(element, kAXIdentifierAttribute as CFString) == "send_message" {
                    sendItem = element
                    return true
                }
                return false
            }
        }
    }
    return sendItem
}

// Fallback for window states where Messages does not publish a send control.
// Requires Messages to be frontmost, which messagesApplication() guarantees.
func pressReturn() {
    guard let source = CGEventSource(stateID: .hidSystemState),
          let down = CGEvent(keyboardEventSource: source, virtualKey: 0x24, keyDown: true),
          let up = CGEvent(keyboardEventSource: source, virtualKey: 0x24, keyDown: false)
    else { return }
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.3)
}

func read(displayName: String) throws {
    let (app, axApp) = try messagesApplication()
    // Unknown senders render as "Maybe: Name, …"; saved contacts drop the prefix.
    let prefixes = ["Maybe: \(displayName),", "\(displayName),"]
    var descriptions: [String] = []
    var seen = Set<String>()
    for window in searchRoots(app, axApp) {
        walk(window) { element in
            // The sidebar row mirrors the latest message using the same sender
            // prefix. It is conversation metadata, not a transcript balloon.
            if stringAttribute(element, kAXIdentifierAttribute as CFString) == "ConversationList" {
                return true
            }
            guard let description = stringAttribute(element, kAXDescriptionAttribute as CFString),
                  prefixes.contains(where: description.hasPrefix),
                  !seen.contains(description) else {
                return false
            }
            seen.insert(description)
            descriptions.append(description)
            // A matching balloon container contains a duplicate sticker child.
            return true
        }
    }
    let data = try JSONSerialization.data(withJSONObject: descriptions)
    FileHandle.standardOutput.write(data)
}

func actionNames(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success else { return [] }
    return (names as? [String]) ?? []
}

// What the composer and the surrounding controls actually support, so the send
// path can be chosen from evidence rather than assumption.
func probe() throws {
    let (app, axApp) = try messagesApplication()
    var lines: [String] = []
    for window in searchRoots(app, axApp) {
        walk(window) { element in
            let role = stringAttribute(element, kAXRoleAttribute as CFString) ?? "?"
            let identifier = stringAttribute(element, kAXIdentifierAttribute as CFString) ?? ""
            let actions = actionNames(element)
            let interesting = identifier == "messageBodyField"
                || identifier.contains("send")
                || (role == (kAXButtonRole as String) && !actions.isEmpty)
            if interesting {
                let description = stringAttribute(element, kAXDescriptionAttribute as CFString) ?? ""
                lines.append("\(role) id=\(identifier) desc=\(description) actions=\(actions)")
            }
            return false
        }
    }
    FileHandle.standardOutput.write(Data(lines.joined(separator: "\n").utf8))
}

// Every bubble in the open thread, sent and received, for diagnosing whether a
// prompt actually left the composer.
func transcript() throws {
    let (app, axApp) = try messagesApplication()
    var descriptions: [String] = []
    var seen = Set<String>()
    for window in searchRoots(app, axApp) {
        walk(window) { element in
            if stringAttribute(element, kAXIdentifierAttribute as CFString) == "ConversationList" {
                return true
            }
            guard let description = stringAttribute(element, kAXDescriptionAttribute as CFString),
                  !description.isEmpty, !seen.contains(description) else {
                return false
            }
            seen.insert(description)
            descriptions.append(description)
            return false
        }
    }
    let data = try JSONSerialization.data(withJSONObject: descriptions)
    FileHandle.standardOutput.write(data)
}

do {
    guard CommandLine.arguments.count >= 2 else { throw BridgeError.usage }
    switch CommandLine.arguments[1] {
    case "send":
        let data = FileHandle.standardInput.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8), !text.isEmpty else {
            throw BridgeError.usage
        }
        try send(text)
    case "read":
        try read(displayName: CommandLine.arguments.count > 2
            ? CommandLine.arguments[2] : "Instinct")
    case "transcript":
        try transcript()
    case "probe":
        try probe()
    default:
        throw BridgeError.usage
    }
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(1)
}
