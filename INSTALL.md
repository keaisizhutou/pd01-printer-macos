# 给本机 AI 代理的安装与验收清单

用户把仓库地址交给你并要求「自己装好、启动」时，按本清单执行本机命令、读取结果并处理问题。用户只需在系统权限、实物操作和打印确认环节接手。你需要具备朋友 Mac 上的终端权限；仅能聊天或运行在远程云环境的 AI 无法直接安装到这台 Mac、使用它的蓝牙。

## 1. 确认环境与源码

- 当前支持范围是 macOS、Python 3.12、已登录的桌面会话、带 Swift 编译器的 Apple Command Line Tools。保留已有 Python 和其他项目的环境。
- 先定位已有仓库，检查 Git 状态。不要重置用户改动或用新克隆覆盖已有目录。没有仓库时再克隆，或使用用户下载并解压的源码。
- 仓库仍为私有时，朋友必须先有访问权限。区分没有权限、没有 Git、网络失败；不要要求把 Token、私钥或 Cookie 粘贴到对话或项目文件。

```bash
uname -s
python3.12 --version
python3.12 -c "import platform; print(platform.machine())"
xcrun --find swiftc
```

如果缺少 Python 3.12，可按用户允许的本机安装方式准备。缺少 Apple 工具时，可运行 `xcode-select --install` 打开系统安装流程；系统许可与安装对话框需要用户完成。完成前不要声称确认窗口可用。没有 Git 时也可从 GitHub 下载 ZIP，项目运行不依赖 Git。

### Intel 与 M 系列

- M 系列使用原生 `arm64` Python。锁定的 `cryptography 50.0.2` 有官方 ARM64 Mac 预编译包，通常无需本地编译；若机器是 M 系列但 Python 显示 `x86_64`，先检查 Rosetta 和 Python 架构，不强行安装不匹配的轮子。
- Intel 使用 `x86_64` Python。当前 `cryptography` 已停止正式支持 Intel Mac 并停止发布对应轮子，本项目的 Intel 路径需要单独验证源码构建；这可能需要额外构建工具及更多时间。遇到构建错误时，按错误和[该版本官方安装说明](https://cryptography.io/en/50.0.2/installation/)检查 Rust、C 编译器及 OpenSSL，不把它误判为蓝牙故障，也不为了绕过编译随意降低加密依赖版本。
- 使用 Homebrew 且缺少源码构建工具时，可按官方说明准备 `openssl@3` 和 `rust`，并用 `brew --prefix openssl@3` 指定本次构建的 `OPENSSL_DIR`。不要假定每台 Mac 都已有这些工具。

这是软件依赖的平台差异；M 系列的蓝牙、系统授权及实物出纸仍需独立验证。CI 分别在 ARM64 与 Intel Mac 环境安装、编译确认程序及运行不出纸测试。

```bash
git clone https://github.com/keaisizhutou/pd01-printer-macos.git
cd pd01-printer-macos
```

## 2. 在项目虚拟环境安装已验证依赖

已有 `.venv` 时，先检查其 Python 版本，确认属于本项目。发现版本不符或环境损坏时，说明原因并保留旧环境，不自动删除。新安装使用：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip check
.venv/bin/python pd01_mac.py setup-confirmation
```

使用完整的哈希锁文件安装；`requirements.txt` 保留三个直接依赖的版本作为维护输入。后续命令显式使用 `.venv/bin/python`，不依赖终端是否激活了某个环境。网络或安装失败时读取具体错误，不用「启动成功」替代安装结果。

## 3. 先做不出纸的软件验收

```bash
.venv/bin/python -m unittest discover -s tests -v
```

自动测试使用模拟蓝牙与临时数据。它们覆盖真实本机 HTTP 启动、MCP 握手、十个工具的发现、预览与任务状态读回、无批准时不能打印，以及已有故障与队列规则。不把测试中的假批准用到真实任务，不额外打印来验证安装。

设置好工作目录及本机数据目录后，检查计划使用的端口。默认是 `127.0.0.1:4176`。不要关闭来源不明的占用进程：

- `GET /health` 可返回当前进程的服务、源码、数据目录、代码和 Python 环境指纹。
- 与本次安装的预期身份完全相符时，可以复用已运行的服务。
- 返回 404、无关内容或身份不同，说明这个端口不能直接复用。先查明原服务，或为本次服务明确指定另一个空闲的 `MCP_PORT`。
- `/health` 不查询蓝牙，也不证明打印机在线或实际出纸。浏览器访问 `/mcp` 出现 400/406 不能作为健康检查结论。

## 4. 启动并保留服务

在你能保留的本机终端会话中运行，保存该会话句柄，保持窗口或进程存在：

```bash
MCP_HOST=127.0.0.1 MCP_PORT=4176 .venv/bin/python printer_server.py
```

如果你的工具必须用后台进程启动，日志放在用户自己的私有数据目录，记录你本次真正创建的 PID，并核对健康响应。不要将本机路径、日志、PID、任务或设备配置提交到 Git。停止时只操作自己启动且身份已核对的进程；旧 PID 文件本身不是杀进程的依据。

服务启动后，用同一项目 `.venv`、同一 `PD01_STATE_DIR` 和端口进行身份读回。下面示例使用默认端口；改了端口时也改这里：

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
import urllib.request
import job_store
from server_info import service_identity

expected = service_identity(Path.cwd(), job_store.default_state())
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:4176/health', timeout=3) as response:
    actual = json.loads(response.read(16384))
assert actual == expected, '端口上的服务身份与本次安装不一致'
print('服务身份核对通过；尚未检查打印机')
PY
```

再使用真实 Streamable HTTP MCP 客户端握手并读回预览，不调用打印：

```bash
.venv/bin/python - <<'PY'
import asyncio
from fastmcp import Client

async def main():
    async with Client('http://127.0.0.1:4176/mcp') as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {'list_printers', 'select_printer', 'check_printer', 'prepare_text',
                'prepare_image', 'prepare_mixed', 'get_job_status',
                'report_blank_output', 'print_prepared', 'disconnect_printer'} <= names
        preview = await client.call_tool('prepare_text', {'text': '安装验收预览，不打印'})
        job = preview.structured_content
        assert job.get('prepared') is True, job
        assert any(block.type == 'image' for block in preview.content)
        result = await client.call_tool('get_job_status', {'job_id': job['job_id']})
        assert result.structured_content['status'] == 'prepared'
        print('MCP 握手、预览和任务状态通过；未打印')

asyncio.run(main())
PY
```

## 5. 朋友首次连接打印机

请用户开机、装好纸、热敏面朝向打印头、压好纸并扣紧纸仓，关闭手机打印 App。遇到 macOS 蓝牙许可提示，由用户允许实际运行程序的终端或应用；拒绝后按「系统设置 → 隐私与安全性 → 蓝牙」处理。

```bash
.venv/bin/python pd01_mac.py printers
```

只有一台且没有绑定时，`check` 可以保存该台选择；有多台同名设备时，展示列表并让用户明确选择，不替用户猜测：

```bash
.venv/bin/python pd01_mac.py select-printer <列表中的设备编号>
.venv/bin/python pd01_mac.py check
```

没有发现设备时，软件可以已经安装并运行，报告「软件就绪，设备连接待完成」。不要改连另一台同名设备，不把未验证的纸仓状态说成正常。已有设备规则不能用于另一台打印机；首次没有规则时，说明故障检测尚未校准。用户需要时再按 README 的四组只读步骤校准。

## 6. 交付与验收口径

向用户给出简短结果：

- 项目位置、使用的 Python、依赖和测试是否通过。
- 实际服务地址、进程或终端会话，以及身份和 MCP 读回结果。
- 打印机是否已选择、是否读到状态；缺纸／纸仓检查是否经过本机校准。
- 需要用户完成的系统授权或实物操作。

将 `http://127.0.0.1:4176/mcp` 填入支持 Streamable HTTP 的本机 AI 客户端。客户端与服务须在同一台 Mac，或另行配置安全网络入口；远程云 AI 的 `127.0.0.1` 不指向朋友的 Mac。

用户只要求安装和启动时，到软件验收与连接状态说明为止。用户明确要求试打印后，再准备新任务、展示完整预览，调用现有打印流程。只有用户点击本机按钮才会发送；结果不确定时不自动重打。正常 HTTP、状态回包和 `data_sent` 都不能替代用户确认实物。
