# PD01 Printer for macOS

让 Mac 通过蓝牙连接 PD01 热敏打印机：检查连接、排版中文、查看 PNG 预览、确认后打印。提供命令行入口，并保留原项目的 Streamable HTTP MCP 服务。

基于 [1124823599331l-boop/pd01-printer-mcp](https://github.com/1124823599331l-boop/pd01-printer-mcp) 的 Mac 适配，沿用原项目的蓝牙、打印协议和排版代码，并非从零重写驱动。原作者署名与 MIT 许可证见 [NOTICE.md](NOTICE.md) 和 [LICENSE](LICENSE)。

## 安装

需要带蓝牙的 Mac、Python 3.12、兼容原项目 0x5178 协议的 PD01，以及 58mm 热敏纸。

```bash
git clone https://github.com/keaisizhutou/pd01-printer-macos.git
cd pd01-printer-macos
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

打开打印机、装纸，关闭占用它的手机打印 App。开启 Mac 蓝牙。首次运行如果出现蓝牙访问提示，请允许正在运行程序的终端或应用；被拒绝时到「系统设置 → 隐私与安全性 → 蓝牙」检查权限。Mac 使用程序扫描 BLE，不必照搬 Windows 的手动配对步骤。

## 先检查，再预览和打印

```bash
python pd01_mac.py check
python pd01_mac.py prepare --text-file ./message.txt --font-size 36 --align center
```

`message.txt` 使用 UTF-8 编码。`prepare` 返回原文、字号、任务 ID 和本地 PNG 路径，不连接打印机、不出纸。默认字号为 36；可用范围 12–72，长行自动换行。需要特殊分行时，在原文中加入换行。

查看 PNG 并确认原文和布局后，用返回的任务 ID 执行：

```bash
python pd01_mac.py print <任务ID> --confirm CONFIRM_PRINT
```

同一任务只允许尝试一次。发送异常、超时或结果不明确时，不自动重打；先看实物。确实需要另一张时，重新 `prepare` 生成新任务，并重新确认。

打印返回 `data_sent` 只说明蓝牙数据发送完成，不能证明纸张完整输出；程序也不读取缺纸、电量或过热状态。`check` 验证蓝牙连接和写入通道。

## 本地数据

设备标识、原文、预览和打印尝试记录默认保存在用户目录的 `Library/Application Support/pd01-printer-macos/`，不在仓库里。可通过全局参数 `--state-dir` 指定其他私有目录。不要将这些数据、真实设备地址或凭证提交到 GitHub。

Mac 蓝牙使用系统分配的设备 UUID；自检纸上的蓝牙 MAC 地址不能直接照填为 Mac 的设备 ID。存在多台同名设备时，CLI 拒绝随意选择；关闭其他设备后重新检查，或设置 `PD01_DEVICE_ADDRESS` 为该 Mac 扫描到的 UUID。

## MCP 入口

```bash
python printer_server.py
```

本地地址为 `http://127.0.0.1:4176/mcp`，传输方式为 Streamable HTTP。可用工具：

- `check_printer`：检查连接，不出纸。
- `prepare_text`：准备文字，默认 36 号字，不出纸。
- `print_prepared`：传入任务 ID 和 `CONFIRM_PRINT` 后发送。
- `disconnect_printer`：断开连接。

Mac 字体自动尝试系统黑体等中文字体；也可设置 `PD01_FONT_PATH` 为现有 TTF/TTC 字体路径。服务默认只监听本机；远程访问必须另外配置身份验证和安全网络入口。

**确认的边界：** `CONFIRM_PRINT` 是工作流约定，AI 也能填写，并不能在程序层面证明真人已同意。AI 客户端必须先展示完整内容并获得用户明确批准。CLI 的一次尝试记录是额外保护；原项目 MCP 仍使用内存任务，没有 CLI 的持久化去重保护。确认字符串也不是网络身份验证。

## 验证范围

2026-10-28 已在一台 Mac 和一台 PD01 上完成 BLE 连接，以及使用本仓库所沿用的驱动发送 24、40、36 号中文排版。用户反馈促成了字号调整。该结果不代表所有 PD01 固件或所有 Mac 均兼容；新 CLI 的任务记录和错误处理通过无硬件测试，避免为测试额外出纸。

```bash
python -m unittest discover -s tests -v
```

项目不附带个人设备标识、打印内容、预览图片、日志或访问凭证。
