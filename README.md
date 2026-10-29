# PD01 Printer for macOS

让 Mac 通过蓝牙连接 PD01 热敏打印机：检查连接、排版中文、查看 PNG 预览、确认后打印。提供命令行入口，并保留原项目的 Streamable HTTP MCP 服务。

基于 [1124823599331l-boop/pd01-printer-mcp](https://github.com/1124823599331l-boop/pd01-printer-mcp) 的 Mac 适配，沿用原项目的蓝牙、打印协议和排版代码，并非从零重写驱动。原作者署名与 MIT 许可证见 [NOTICE.md](NOTICE.md) 和 [LICENSE](LICENSE)。

## 安装

需要带蓝牙且已登录桌面的 Mac、Python 3.12、Apple Command Line Tools（含 Swift 编译器）、兼容原项目 0x5178 协议的 PD01，以及 58mm 热敏纸。缺少编译器时先手动运行 `xcode-select --install`。

```bash
git clone https://github.com/keaisizhutou/pd01-printer-macos.git
cd pd01-printer-macos
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python pd01_mac.py setup-confirmation
```

打开打印机、装纸，关闭占用它的手机打印 App。开启 Mac 蓝牙。首次运行如果出现蓝牙访问提示，请允许正在运行程序的终端或应用；被拒绝时到「系统设置 → 隐私与安全性 → 蓝牙」检查权限。Mac 使用程序扫描 BLE，不必照搬 Windows 的手动配对步骤。

## 先检查，再预览和打印

```bash
python pd01_mac.py check
python pd01_mac.py prepare --text-file ./message.txt --font-size 36 --align center
```

图片也可以先准备成黑白点阵预览：

```bash
python pd01_mac.py prepare-image --image-file ./drawing.png
```

图片按比例缩放至 384 点宽，透明区域按白底处理，再转换为与打印数据一致的黑白预览。图像任务的本机确认窗口显示「黑白图片 · 1 张」，不显示文字字号。返回任务 ID 后仍用同一个 `print` 命令和人工确认流程；准备图片不会出纸。

`message.txt` 使用 UTF-8 编码。`prepare` 返回原文、字号、任务 ID 和本地 PNG 路径，不连接打印机、不出纸。默认字号为 36，居中排版；可用范围 12–72。长行优先在空格或较靠后的句读处换行，尽量避免中文句末标点单独落在行首、开括号落在行尾，以及拆开能放进一行的英文单词。超长英文单词仍会分开。手动换行和空白行保留。

查看 PNG 并确认原文和布局后，用返回的任务 ID 执行。这会打开 Mac 本机确认窗口：

```bash
python pd01_mac.py print <任务ID> --confirm CONFIRM_PRINT
```

窗口显示本次保存的实际图片、字号、对齐方式和 1 张的份数。**只有点击「确认打印 1 张」才会发送到打印机。** 取消、关闭窗口、等待 120 秒、编译失败或没有可用桌面时均不发送。取消后如需重新打印，重新准备一个任务；同一任务不会再次弹窗。首次构建窗口可能较慢，建议先运行上面的 `setup-confirmation`，后续使用本机缓存。

可单独测试窗口，按钮点击后也不连接打印机、不出纸：

```bash
python pd01_mac.py confirm-demo
```

MCP 和命令行共用持久化任务记录；同一任务跨入口、跨进程只允许尝试一次。发送前先以独占方式创建并刷新尝试记录，失败、取消或程序重启后仍保留。发送异常、超时或结果不明确时，不自动重打；先看实物。确实需要另一张时，重新 `prepare` 生成新任务，并重新确认。内容相同的新任务仍是独立任务；这不是按文本去重。

打印返回 `data_sent` 只说明蓝牙数据发送完成，不能证明纸张完整输出；程序也不读取缺纸、电量或过热状态。`check` 验证蓝牙连接和写入通道。

在支持通知的 BLE 连接上，发送前订阅 AE02 并等待有效状态回包；收不到回复就停止，不发送图片。打印数据分包间隔为 20 毫秒，发送结束后保持连接两秒，再查询一次状态，避免立刻断开。发送后无回包会报告结果不确定并阻止自动重试。`device_reply_after_send` 表示收到了设备回复，`device_ready_reported` 表示收到设备就绪／恢复接收的 AE 通知；二者都不能代替用户确认实物出纸。

## 本地数据

设备标识、原文、预览和打印尝试记录默认保存在用户目录的 `Library/Application Support/pd01-printer-macos/`，不在仓库里。MCP 和命令行都可设置环境变量 `PD01_STATE_DIR`；命令行也可通过全局参数 `--state-dir` 指定其他私有目录。跨入口使用同一任务时必须使用相同的数据目录。不要将这些数据、真实设备地址或凭证提交到 GitHub。

Mac 蓝牙使用系统分配的设备 UUID；自检纸上的蓝牙 MAC 地址不能直接照填为 Mac 的设备 ID。存在多台同名设备时，CLI 拒绝随意选择；关闭其他设备后重新检查，或设置 `PD01_DEVICE_ADDRESS` 为该 Mac 扫描到的 UUID。

## MCP 入口

```bash
python printer_server.py
```

本地地址为 `http://127.0.0.1:4176/mcp`，传输方式为 Streamable HTTP。可用工具：

- `check_printer`：检查连接，不出纸。
- `prepare_text`：准备文字，默认 36 号字、居中，返回结构化参数和实际 PNG 图片，不出纸。
- `print_prepared`：传入任务 ID 和 `CONFIRM_PRINT` 后打开本机确认窗口，点击「确认打印 1 张」才尝试发送。
- `disconnect_printer`：断开连接。

Mac 字体自动尝试系统黑体等中文字体；也可设置 `PD01_FONT_PATH` 为现有 TTF/TTC 字体路径。服务默认只监听本机；远程访问必须另外配置身份验证和安全网络入口。

AI 客户端应展示返回的图片和完整原文，再向用户取得明确批准。图片内容与保存的打印任务一致；发送前会校验图片摘要。`prepare_text` 的结构化结果仍含任务 ID、原文、字号、对齐和图片尺寸；图片作为额外的 MCP 内容块返回，能否直接显示取决于客户端。

**本机确认：** `CONFIRM_PRINT` 只用于请求打开确认窗口，不能代替窗口中的按钮。MCP 和 CLI 的发送路径均强制等待本机窗口批准；没有跳过确认的 CLI 参数、环境变量或 HTTP 批准接口。窗口通过私有子进程管道返回与本次任务、图片摘要和随机值绑定的决定；不会保存可复用的批准令牌。MCP 客户端应允许至少 120 秒的工具等待时间，首次使用前先完成窗口构建。

该保护用于防止普通 MCP 调用直接出纸，并不是对抗本机同权限恶意程序的操作系统安全隔离：能修改源码、替换程序或自动操作桌面的程序仍可能绕过。确认机制也不代替网络身份验证。当前版本不限制纸张总长度，也不按内容合并不同任务。

打印接口统一返回 `data_sent` 与 `physical_output_verified: false`，不再使用 `printed: true` 表示实物成功。连接检查、发送和断开在同一 MCP 进程中共用连接锁；不同任务的跨进程硬件队列尚未实现。

## 验证范围

2026-10-28 已在一台 Mac 和一台 PD01 上完成 BLE 连接，以及使用本仓库所沿用的协议发送 24、40、36 号中文排版。用户反馈促成了字号调整。后续共用任务存储、MCP 图片返回及换行改进通过无硬件测试和本机中文预览检查。本机确认功能通过 Swift 编译及无硬件测试，覆盖待确认期间不发送、取消、超时、窗口失败和错误批准信息；测试窗口使用 `confirm-demo`，不为测试额外出纸。该结果不代表所有 PD01 固件或所有 Mac 均兼容。

2026-10-29，用户在纸张重新压好后确认文字与黑白图片均已实际出纸，本机按钮确认到打印的流程已验证。此前未出纸由用户确认与纸张未压住有关；分包延时和回包监听是独立的稳定性、可观察性改进，没有证据表明它们解决了该机械问题。纸仓、缺纸和实际走纸的自动检测尚未针对这台设备验证。

```bash
python -m unittest discover -s tests -v
```

项目不附带个人设备标识、打印内容、预览图片、日志或访问凭证。
