# Attribution and changes

This is a macOS adaptation of [1124823599331l-boop/pd01-printer-mcp](https://github.com/1124823599331l-boop/pd01-printer-mcp), MIT licensed.

Upstream commit: `53352722928f234c21f331d32253d27ae86948a6`.
The original `printer_server.py` blob is `02c2ca5a9a07f00958eacbd07df8250e3226211e`.

The BLE transport, raster protocol, CRC and text renderer originate from upstream. Its BLE fallback already supports non-Windows platforms. This project adds macOS Chinese-font discovery, a default font size of 36, punctuation/word-aware wrapping, MCP PNG previews, a shared persistent one-attempt job store, and an AppKit confirmation window required by both CLI and MCP. Connection cleanup and result semantics have also been adapted. It is an adaptation, not a wholly new printer driver.

The local CLI also supports preparing image files as 384-dot monochrome jobs, with transparent areas composited on white and the same preview/approval/send path as text.

Upstream credits [rhnvrm/catprinter](https://github.com/rhnvrm/catprinter) (MIT) for public reverse-engineering work on command framing and GATT details. The original copyright and MIT license are retained in LICENSE.
