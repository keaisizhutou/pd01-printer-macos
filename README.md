# PD01 Printer for macOS

让 Mac 通过蓝牙连接 PD01 热敏打印机：检查连接、排版中文、查看 PNG 预览、确认后打印。提供命令行入口，并保留原项目的 Streamable HTTP MCP 服务。

基于 [1124823599331l-boop/pd01-printer-mcp](https://github.com/1124823599331l-boop/pd01-printer-mcp) 的 Mac 适配，沿用原项目的蓝牙、打印协议和排版代码，并非从零重写驱动。原作者署名与 MIT 许可证见 [NOTICE.md](NOTICE.md) 和 [LICENSE](LICENSE)。

## 把链接交给 AI 安装

把本仓库地址发给能操作你这台 Mac 终端的 AI，并告诉它：

> 请先阅读 README 和 INSTALL.md，按清单在我的 Mac 上安装、启动并完成不出纸验收。你能执行的命令请自己完成；需要系统授权或我操作打印机时再说明。先不要打印。

[INSTALL.md](INSTALL.md) 是 AI 的执行清单，包含环境检查、隔离安装、服务身份核对、真实 MCP 调用及首次选机。安装和打印连接分别验收。每个人使用自己的设备选择、校准和任务目录，仓库不附带作者的打印机配置。私有仓库需要先具备访问权限。

## 安装

需要带蓝牙且已登录桌面的 Mac、Python 3.12、Apple Command Line Tools（含 Swift 编译器）、兼容原项目 0x5178 协议的 PD01，以及 58mm 热敏纸。缺少编译器时先手动运行 `xcode-select --install`。

M 系列应使用原生 `arm64` Python。Intel 的部分依赖可能需要源码编译，额外构建前提见 [INSTALL.md](INSTALL.md#intel-与-m-系列)；不要把本机已有的工具当作所有朋友机器的默认条件。

```bash
git clone https://github.com/keaisizhutou/pd01-printer-macos.git
cd pd01-printer-macos
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip check
python pd01_mac.py setup-confirmation
```

打开打印机、装纸，关闭占用它的手机打印 App。开启 Mac 蓝牙。首次运行如果出现蓝牙访问提示，请允许正在运行程序的终端或应用；被拒绝时到「系统设置 → 隐私与安全性 → 蓝牙」检查权限。Mac 使用程序扫描 BLE，不必照搬 Windows 的手动配对步骤。

## 先检查，再预览和打印

```bash
python pd01_mac.py check
python pd01_mac.py prepare --text-file ./message.txt --font-size 36 --align center
```

CLI 和 MCP 共用设备选择规则：首次未绑定时，只发现一台同名 PD01 才自动保存选择；发现多台时停止，不取扫描列表中的第一台。可以先查看列表并明确指定：

```bash
python pd01_mac.py printers
python pd01_mac.py select-printer <列表中的设备编号>
python pd01_mac.py check
```

MCP 对应使用 `list_printers`、`select_printer(device_id)`。扫描和选择均不打印；已选择的设备离线时，只报告该设备不可用，不自动换成另一台同名机器。选择保存在本机 `device.json`。设置了 `PD01_DEVICE_ADDRESS` 时它优先，选择另一个编号会明确报错，需先更新或移除该变量。

图片也可以先准备成黑白点阵预览：

```bash
python pd01_mac.py prepare-image --image-file ./drawing.png
```

图片按比例缩放至 384 点宽，透明区域按白底处理，再转换为与打印数据一致的黑白预览。图像任务的本机确认窗口显示「黑白图片 · 1 张」，不显示文字字号。返回任务 ID 后仍用同一个 `print` 命令和人工确认流程；准备图片不会出纸。

文字与图片也可以上下混排。把下面的内容保存为 UTF-8 的 `layout.json`，图片路径相对于该文件所在目录：

```json
{
  "blocks": [
    {"type": "text", "text": "图文示例", "font_size": 36, "align": "center"},
    {"type": "spacer", "height": 12},
    {"type": "image", "path": "drawing.png", "width": 320, "align": "center"},
    {"type": "text", "text": "这里是图片说明。\n这里是第二行。", "font_size": 28}
  ]
}
```

```bash
python pd01_mac.py prepare-mixed --manifest ./layout.json
```

混排合成为一张 384 点宽的实际点阵预览，沿用同一个任务和确认按钮。当前支持上下排列，区块可左对齐、居中、右对齐；不支持文字围绕图片。最多 32 个区块，原图最多 1600 万像素，不接受多帧图片；单张点阵最多 4000 行，超出时明确报错，不截断内容。

`message.txt` 使用 UTF-8 编码。`prepare` 返回原文、字号、任务 ID 和本地 PNG 路径，不连接打印机、不出纸。默认字号为 36，居中排版；可用范围 12–72。长行优先在空格或较靠后的句读处换行，尽量避免中文句末标点单独落在行首、开括号落在行尾，以及拆开能放进一行的英文单词。超长英文单词仍会分开。手动换行和空白行保留。

查看 PNG 并确认原文和布局后，用返回的任务 ID 执行。这会打开 Mac 本机确认窗口：

```bash
python pd01_mac.py print <任务ID> --confirm CONFIRM_PRINT
```

窗口显示本次保存的实际图片、字号、对齐方式、1 张的份数，以及绑定的打印机名称和编号末尾。目标在打开窗口前固定；等待确认或排队期间修改默认设备，不会改变这张任务的目标。**只有点击「确认打印 1 张」才会发送到打印机。** 取消、关闭窗口、等待 120 秒、编译失败或没有可用桌面时均不发送。取消后如需重新打印，重新准备一个任务；同一任务不会再次弹窗。首次构建窗口可能较慢，建议先运行上面的 `setup-confirmation`，后续使用本机缓存。

可单独测试窗口，按钮点击后也不连接打印机、不出纸：

```bash
python pd01_mac.py confirm-demo
```

MCP 和命令行共用持久化任务记录；同一任务跨入口、跨进程只允许尝试一次。发送前先以独占方式创建并刷新尝试记录，失败、取消或程序重启后仍保留。发送异常、超时或结果不明确时，不自动重打；先看实物。确实需要另一张时，重新 `prepare` 生成新任务，并重新确认。内容相同的新任务仍是独立任务；这不是按文本去重。

打印返回 `data_sent` 只说明蓝牙数据发送完成，不能证明纸张完整输出。`check` 验证蓝牙连接和写入通道；`status` 查询设备状态。纸张、纸仓等状态只按本机对照验证过的规则解释，未验证的能力显示未知，不套用其他机型的故障位。

在支持通知的 BLE 连接上，发送前订阅 AE02 并等待有效状态回包；收不到回复就停止，不发送图片。打印数据分包间隔为 20 毫秒，发送结束后保持连接两秒，再查询一次状态，避免立刻断开。发送后无回包会报告结果不确定并阻止自动重试。`device_reply_after_send` 表示收到了设备回复，`device_ready_reported` 表示收到设备就绪／恢复接收的 AE 通知；二者都不能代替用户确认实物出纸。

## 排队与超时

人工确认窗口等待期间不占用蓝牙队列。点击确认后，任务先进入「等待打印机」阶段，轮到后再连接并检查状态。不同数据目录仍共用用户目录 `Library/Application Support/pd01-printer-macos/hardware.lock`；锁文件保留，文件存在不代表设备忙，进程退出后系统会释放锁。排队按锁可用性调度，不保证严格先进先出；其他电脑、手机 App 和旧版本程序不在该队列内。

| 阶段 | 上限 |
| --- | --- |
| 等待蓝牙队列 | 120 秒 |
| 单次扫描 | 12 秒 |
| 建立连接 | 20 秒 |
| 启用状态通知 | 5 秒 |
| 每次蓝牙写入 | 5 秒 |
| 每次等待 A3 状态回复 | 3 秒 |
| 停止通知、断开连接 | 每步 3 秒 |
| 状态查询整体，不含排队及清理 | 90 秒 |
| 打印操作整体，不含按钮确认、排队及清理 | 120 秒 |

超时会返回发生阶段。发送前超时明确标为未发送；发送已开始后超时标为结果不确定，不自动续传或重打。停止通知失败时仍会尝试断开；清理失败会报告，原有故障不会被清理错误覆盖。应用时限覆盖异步蓝牙等待，仍依赖系统及底层库响应取消，不能强制中断卡死的同步系统调用。

升级后重启已运行的 MCP 服务，让它加载新队列、选机工具和超时逻辑。队列超时、取消或故障后，旧任务的单次尝试记录仍保留；需要另一张时重新准备并再次点击本机按钮。

## 故障反馈与本机验证

```bash
python pd01_mac.py status
```

状态查询只发送 A3 查询指令，不打印、不走纸。连接不可用、状态超时、传输中断和发送后失联会返回错误代码、中文说明、处理建议，以及结果是否不确定。MCP 的 `check_printer` 也使用同一状态查询。

可反馈的情况及处理方式：

| 情况 | 反馈与处理 |
| --- | --- |
| 没发现打印机、扫描或连接失败 | 提示检查电源、距离、蓝牙权限和手机 App 占用；不将某个原因当作已确定事实 |
| 状态查询无有效回复、通知通道不可用 | 告知当前无法判断设备状态；已启用的故障检查不能执行时阻止打印 |
| 本机已验证的缺纸／纸仓故障 | 发送前拦截；发送期间收到异常时停止后续分包，保留异常，不因后续恢复回包而抹掉 |
| 发送中断、发送后无状态回复 | 提示可能已经部分出纸，不能自动重打 |
| 图片不存在、损坏、多帧、过大、混排格式错误或超长 | 在准备预览时说明具体问题，不连接打印机 |
| 确认取消、关闭、超时、构建或窗口失败 | 区分取消与失败，均不发送 |
| 任务不存在、预览被改动、同一任务重复执行 | 阻止发送；重复任务需先检查上次结果 |
| 本机磁盘满、权限不足或目录只读 | 提示处理本机存储；发送已开始时保留结果不确定的语义 |
| 已配置的本机校准记录损坏或与设备不匹配 | 阻止打印，提示检查或重新校准，不悄悄跳过检查 |

运行 `python pd01_mac.py job-status <任务ID>` 或 MCP `get_job_status` 可以查看本机最近记录的任务阶段和错误建议，包括待确认、设备检查、发送、等待回包、已发送、取消、被拦截及结果不确定。它读取持久化记录，不代表正在实时监视设备；程序被强制关闭后，最后一个阶段仍会保留。`physical_output_verified` 始终为 `false`，软件不能据此声称已实际出纸。

### 走纸但空白

用户反馈「出了白纸」「走纸了但没有字或图」时，AI 可调用 `report_blank_output(job_id)`；命令行也可以记录该情况并获取排查提示：

```bash
python pd01_mac.py report-blank <任务ID>
python pd01_mac.py job-status <任务ID>
```

提示依次检查热敏面朝向、纸是否压好与纸仓盖是否扣紧、是否使用适配的热敏纸；仍有问题时建议按设备说明做自检对照。程序同时检查该任务保存的预览是否包含黑色内容，预览本身为空时先提示重新准备内容。热敏纸辨认与装纸的一般排查依据见 [Star Micronics 支持说明](https://starmicronics.com/help-center/knowledge-base/how-to-troubleshoot-blank-printing-on-thermal-printers/)，具体装纸方向以 PD01 本机说明为准。

这是客户端转述症状后生成的排查建议，不是自动检测到纸放反；返回 `sensor_confirmed: false`。反馈保存在对应任务的本机记录，之后查询任务状态可读到。未发送、已取消或发送前被拦截的任务不能记录成出白纸。这个入口不会连接打印机、出纸、改变原发送结果或解除单次尝试限制；调整后需另建任务并重新点击本机按钮。

只在设备暴露可读的标准 Battery Level 特征、且返回值有效时显示电量百分比。接口不存在、读取失败或数据异常时返回未知，不显示成 0%。本机 PD01 实际连接查询未发现该标准特征，因此当前无法读取电量；其他私有接口尚未验证。未定义未经验证的低电量阈值，也不将 A3 后面的未知字节解释为电量或温度。标准定义见 [Bluetooth Battery Service](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BAS_v1.1/out/en/index-en.html)。

纸仓和缺纸的对照步骤如下，每步需由操作者先完成实物操作，再运行对应命令：

```bash
# 有纸、压好并扣紧纸仓
python pd01_mac.py status --samples 3 --sample-label normal
# 打开纸仓盖，纸留在里面
python pd01_mac.py status --samples 3 --sample-label cover_open
# 取出纸卷，重新扣紧空纸仓
python pd01_mac.py status --samples 3 --sample-label paper_missing
# 装回纸、压好并重新扣紧纸仓
python pd01_mac.py status --samples 3 --sample-label recovered
python pd01_mac.py calibrate-status
```

四组数据必须来自同一台设备。只从有协议依据的首个状态字节提取可重复变化的标志；不把后面可能变化的读数猜成故障。恢复状态必须回到正常基准，否则拒绝启用规则。开盖与缺纸若无法区分，只提示「缺纸或纸仓未扣好」；未观察到稳定差异的情况保持未验证。

规则保存在本机 `status-profile.json`，绑定当前设备，不随代码上传或自动迁移到其他打印机。规则启用后，发送前检测到已验证故障会阻止打印数据，发送期间检测到故障会停止后续分包并提示结果不确定；程序不会自动重打。错误及处理建议保存在该任务本机记录中。

检测到传感器标志不代表能判断纸是否实际压紧、纸面朝向、是否完整走纸或图案是否完整。未获得相应硬件证据前，这些项目仍不能自动确认。

## 本地数据

设备标识、原文、预览和打印尝试记录默认保存在用户目录的 `Library/Application Support/pd01-printer-macos/`，不在仓库里。MCP 和命令行都可设置环境变量 `PD01_STATE_DIR`；命令行也可通过全局参数 `--state-dir` 指定其他私有目录。跨入口使用同一任务时必须使用相同的数据目录。不要将这些数据、真实设备地址或凭证提交到 GitHub。

Mac 蓝牙使用系统分配的设备 UUID；自检纸上的蓝牙 MAC 地址不能直接照填为 Mac 的设备 ID。存在多台同名设备时，CLI 和 MCP 均拒绝随意选择；使用扫描列表中的编号明确选择目标。

## MCP 入口

```bash
python printer_server.py
```

本地地址为 `http://127.0.0.1:4176/mcp`，传输方式为 Streamable HTTP。可用工具：

- `list_printers`：扫描同名设备并返回本机编号，不连接或打印。
- `select_printer`：从扫描列表明确选择设备，用于之后的新任务，不打印。
- `check_printer`：查询设备状态并返回本机已验证的故障提示，不出纸。
- `prepare_text`：准备文字，默认 36 号字、居中，返回结构化参数和实际 PNG 图片，不出纸。
- `prepare_image`：传入本机图片路径 `image_file`，准备黑白图片并返回实际 PNG 预览，不出纸。
- `prepare_mixed`：传入上述 `blocks` 及图片相对路径的 `base_dir`，准备图文混排与实际 PNG 预览，不出纸。
- `get_job_status`：读取任务最近记录的阶段、错误原因和处理建议，不连接打印机。
- `report_blank_output`：仅在用户反馈走纸但空白时调用，记录客户端反馈并返回装纸、热敏面及预览检查建议；不连接设备，不重打。
- `print_prepared`：传入任务 ID 和 `CONFIRM_PRINT` 后打开本机确认窗口，点击「确认打印 1 张」才尝试发送。
- `disconnect_printer`：断开连接。

安装验收还可读取 `http://127.0.0.1:4176/health`。它只返回运行进程的服务、源码、数据目录、代码和 Python 环境指纹，用于识别端口上的服务是否属于本次安装；不返回原始路径、设备编号或打印内容，也不检查打印机。核对方式见 INSTALL，不能用 HTTP 正常代替实际 MCP 调用与实物验收。

Mac 字体自动尝试系统黑体等中文字体；也可设置 `PD01_FONT_PATH` 为现有 TTF/TTC 字体路径。服务默认只监听本机；远程访问必须另外配置身份验证和安全网络入口。

AI 客户端应展示返回的图片和完整原文，再向用户取得明确批准。图片内容与保存的打印任务一致；发送前会校验图片摘要。`prepare_text` 的结构化结果仍含任务 ID、原文、字号、对齐和图片尺寸；图片作为额外的 MCP 内容块返回，能否直接显示取决于客户端。

**本机确认：** `CONFIRM_PRINT` 只用于请求打开确认窗口，不能代替窗口中的按钮。MCP 和 CLI 的发送路径均强制等待本机窗口批准；没有跳过确认的 CLI 参数、环境变量或 HTTP 批准接口。窗口通过私有子进程管道返回与本次任务、图片摘要和随机值绑定的决定；不会保存可复用的批准令牌。窗口按钮等待上限仍为 120 秒；单次 MCP 工具调用还可能包含排队、扫描和传输，建议客户端允许 600 秒，首次使用前先完成窗口构建。

该保护用于防止普通 MCP 调用直接出纸，并不是对抗本机同权限恶意程序的操作系统安全隔离：能修改源码、替换程序或自动操作桌面的程序仍可能绕过。确认机制也不代替网络身份验证。每个任务有点阵长度上限，但不累计限制不同任务的总用纸量，也不按内容合并不同任务。

打印接口统一返回 `data_sent` 与 `physical_output_verified: false`，不再使用 `printed: true` 表示实物成功。扫描、选择、状态查询、发送和断开共用跨进程蓝牙队列；同一 Mac 用户运行的 CLI 和 MCP 会串行使用蓝牙，即便它们使用不同的本机数据目录。

## 验证范围

2026-10-28 已在一台 Mac 和一台 PD01 上完成 BLE 连接，以及使用本仓库所沿用的协议发送 24、40、36 号中文排版。用户反馈促成了字号调整。后续共用任务存储、MCP 图片返回及换行改进通过无硬件测试和本机中文预览检查。本机确认功能通过 Swift 编译及无硬件测试，覆盖待确认期间不发送、取消、超时、窗口失败和错误批准信息；测试窗口使用 `confirm-demo`，不为测试额外出纸。该结果不代表所有 PD01 固件或所有 Mac 均兼容。

2026-10-29，用户在纸张重新压好后确认文字与黑白图片均已实际出纸，本机按钮确认到打印的流程已验证。此前未出纸由用户确认与纸张未压住有关；分包延时和回包监听是独立的稳定性、可观察性改进，没有证据表明它们解决了该机械问题。

同日完成一台 PD01 的正常装纸、开盖留纸、无纸关盖、恢复装纸四组只读查询，每组至少三次。开盖与缺纸共用异常标志，恢复后回到正常基准；因此本机规则只能合并反馈「缺纸或纸仓未扣好」。该规则已在此设备本机启用，代码不会把它当作所有 PD01 的通用规则。四组回包通过同一故障判定函数回放验证，异常会在打印数据发送前被拦截；没有为验证额外打印。纸张是否实际压紧、卡纸、低电量、过热及实际走纸仍未完成设备验证。

扩展反馈、混排与验证边界见 [VALIDATION.md](VALIDATION.md)。混排经过本机预览、无硬件流程及实物出纸验证，用户在调整纸卷方向后确认文字与小猫图片清楚完整。前后两张点阵相同，显色异常及正常打印时设备均未报告已验证故障，现有状态回包不能据此识别纸面方向或显色质量。软件不会为了测试故意造成卡纸、过热或过放电。

```bash
python -m unittest discover -s tests -v
```

项目不附带个人设备标识、打印内容、预览图片、日志或访问凭证。

## 维护与依赖更新

`requirements.txt` 是三个直接依赖的维护输入，`requirements.lock` 锁定完整依赖及分发包哈希。普通安装使用后者。修改依赖后，维护者可以用 uv 重新解析，并更新输入摘要：

```bash
uv pip compile requirements.txt --python-version 3.12 --universal --generate-hashes --no-annotate --no-header -o requirements.lock
python3.12 - <<'PY'
import hashlib
from pathlib import Path
path = Path('requirements.lock')
body = '\n'.join(line for line in path.read_text().splitlines() if not line.startswith('# source-sha256:'))
digest = hashlib.sha256(Path('requirements.txt').read_bytes()).hexdigest()
path.write_text('# source-sha256: ' + digest + '\n' + body + '\n')
PY
```

依赖升级需重新在空虚拟环境安装、执行 `pip check`、构建确认窗口并运行全部测试。GitHub Actions 使用 Mac 环境执行相同流程；测试禁止真实蓝牙和真实打印，不能代替其他 Mac、固件及实物的兼容性验证。
