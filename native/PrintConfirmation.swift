import AppKit
import CryptoKit

struct Request: Decodable {
    let jobID: String
    let nonce: String
    let sha256: String
    let image: String
    let fontSize: Int
    let align: String
    let purpose: String
}

final class PreviewCanvas: NSView {
    override var isFlipped: Bool { true }
}

final class ConfirmationController: NSObject, NSWindowDelegate {
    let request: Request
    let image: NSImage
    let pixelSize: NSSize
    var window: NSWindow!
    var finished = false
    var confirmButton: NSButton!

    init(request: Request, png: Data, bitmap: NSBitmapImageRep) {
        self.request = request
        self.pixelSize = NSSize(width: bitmap.pixelsWide, height: bitmap.pixelsHigh)
        self.image = NSImage(size: self.pixelSize)
        super.init()
        image.addRepresentation(bitmap)
    }

    func show() {
        let isDemo = request.purpose == "demo"
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 580, height: 740),
                          styleMask: [.titled, .closable, .miniaturizable],
                          backing: .buffered, defer: false)
        window.title = isDemo ? "PD01 · 确认窗口测试（不打印）" : "PD01 · 确认打印"
        window.delegate = self
        window.isReleasedWhenClosed = false
        let root = NSView()
        window.contentView = root

        let heading = NSTextField(labelWithString: isDemo ? "确认窗口测试" : "检查后，再打印")
        heading.font = .systemFont(ofSize: 24, weight: .semibold)
        let subtitle = NSTextField(wrappingLabelWithString: isDemo
            ? "这是窗口测试。点击下方按钮也不会连接打印机或出纸。"
            : "请核对下面的实际预览。点击确认后，将向打印机发送 1 张。")
        subtitle.textColor = .secondaryLabelColor
        let alignment = ["left": "左对齐", "center": "居中", "right": "右对齐"][request.align] ?? request.align
        let details = NSTextField(labelWithString: "字号 \(request.fontSize)  ·  \(alignment)  ·  1 张")
        details.font = .systemFont(ofSize: 13, weight: .medium)

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.drawsBackground = true
        scroll.backgroundColor = .underPageBackgroundColor
        let canvas = PreviewCanvas(frame: NSRect(x: 0, y: 0, width: 528,
                                                 height: max(410, pixelSize.height + 40)))
        let paper = NSImageView(frame: NSRect(x: (528 - pixelSize.width) / 2, y: 20,
                                             width: pixelSize.width, height: pixelSize.height))
        paper.image = image
        paper.imageScaling = .scaleNone
        paper.setAccessibilityLabel("本次打印的实际预览")
        canvas.addSubview(paper)
        scroll.documentView = canvas

        let note = NSTextField(wrappingLabelWithString: "等待确认时不会出纸。关闭窗口、取消或等待超时均取消本次任务。")
        note.font = .systemFont(ofSize: 12)
        note.textColor = .secondaryLabelColor
        let cancel = NSButton(title: "取消", target: self, action: #selector(cancelled))
        cancel.bezelStyle = .rounded
        cancel.keyEquivalent = "\u{1b}"
        confirmButton = NSButton(title: isDemo ? "确认测试（不打印）" : "确认打印 1 张",
                                 target: self, action: #selector(approved))
        confirmButton.bezelStyle = .rounded
        // No Return-key shortcut: opening a window or pressing Enter never approves it.
        confirmButton.keyEquivalent = ""
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let buttons = NSStackView(views: [spacer, cancel, confirmButton])
        buttons.orientation = .horizontal
        buttons.spacing = 12
        let stack = NSStackView(views: [heading, subtitle, details, scroll, note, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 24),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -24),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -24),
            subtitle.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 350),
            note.widthAnchor.constraint(equalTo: stack.widthAnchor),
            buttons.widthAnchor.constraint(equalTo: stack.widthAnchor),
            confirmButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 144)
        ])
        window.defaultButtonCell = nil
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func approved() { finish(approved: true) }
    @objc func cancelled() { finish(approved: false) }
    func windowWillClose(_ notification: Notification) { finish(approved: false) }

    func finish(approved: Bool) {
        guard !finished else { return }
        finished = true
        confirmButton.isEnabled = false
        let response: [String: Any] = [
            "approved": approved, "job_id": request.jobID,
            "nonce": request.nonce, "sha256": request.sha256, "purpose": request.purpose
        ]
        if let data = try? JSONSerialization.data(withJSONObject: response) {
            FileHandle.standardOutput.write(data)
        }
        exit(0)
    }
}

do {
    let request = try JSONDecoder().decode(Request.self, from: FileHandle.standardInput.readDataToEndOfFile())
    guard ["print", "demo"].contains(request.purpose),
          let png = Data(base64Encoded: request.image),
          let bitmap = NSBitmapImageRep(data: png) else { exit(2) }
    let digest = SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined()
    guard digest == request.sha256 else { exit(2) }
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let controller = ConfirmationController(request: request, png: png, bitmap: bitmap)
    controller.show()
    withExtendedLifetime(controller) { app.run() }
} catch {
    exit(2)
}
