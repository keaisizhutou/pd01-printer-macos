# Attribution and changes

This is a macOS adaptation of [1124823599331l-boop/pd01-printer-mcp](https://github.com/1124823599331l-boop/pd01-printer-mcp), MIT licensed.

Upstream commit: `53352722928f234c21f331d32253d27ae86948a6`.
The original `printer_server.py` blob is `02c2ca5a9a07f00958eacbd07df8250e3226211e`.

The BLE transport, raster protocol, CRC and text renderer are reused from upstream. Its BLE fallback already supports non-Windows platforms. This project adds macOS Chinese-font discovery, a default MCP font size of 36, and a local CLI with PNG previews and persistent one-attempt job records. It is an adaptation, not a wholly new printer driver.

Upstream credits [rhnvrm/catprinter](https://github.com/rhnvrm/catprinter) (MIT) for public reverse-engineering work on command framing and GATT details. The original copyright and MIT license are retained in LICENSE.
