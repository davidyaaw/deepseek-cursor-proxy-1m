<h1 align="center"><img src="assets/logo.png" width="150" alt="deepseek-cursor-proxy logo"><br>DeepSeek Cursor Proxy</h1>

<p align="center"><a href="README.md">English</a> | <a href="README.ru.md">Русский</a> | <b>简体中文</b></p>

在 Cursor 里使用 DeepSeek，上下文窗口为 **1,000,000 token**。并行 **子代理各自保留对话和 thinking**，不再共用同一份推理缓存。

这是 [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy) 的分支（MIT）。原项目修复工具调用时的 `reasoning_content` 错误。本分支增加 1M 窗口、Cursor 子代理隔离，以及一键启动。

## 安装

需要 [uv](https://docs.astral.sh/uv/)、[ngrok](https://ngrok.com/)（执行一次 `ngrok config add-authtoken`）和 [DeepSeek API 密钥](https://platform.deepseek.com/api_keys)。

**Windows。** 克隆本仓库，双击 `Start DeepSeek Proxy.cmd`。窗口保持打开。启动器会打印 Base URL 并复制到剪贴板。

**macOS / Linux。**

```bash
git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
./start-deepseek-proxy.sh
```

首次运行会创建 `~/.deepseek-cursor-proxy/config.yaml`。设置 Cursor 请求要改写成的模型：

```yaml
model: deepseek-flash
```

## 连接 Cursor

1. 打开 **Settings → Models → API Keys**
2. 启用 **Override OpenAI Base URL**，粘贴启动器给出的地址，必须以 `/v1` 结尾。
3. 在 **OpenAI API Key** 中填入 DeepSeek 密钥（`sk-...`）。
4. 在模型选择器里选 **GPT-5.6 Sol**。Cursor 按 1M 分配预算，实际由 DeepSeek 回答。
5. 用 `Ctrl+Shift+0`（Windows/Linux）或 `Cmd+Shift+0`（macOS）开关自定义 API。

<img src="assets/cursor_config.png" width="600" alt="Cursor 中的 API 密钥和 Base URL 设置">

第一次对话之后，也可以选择名为 `deepseek-flash` 或 `deepseek-v4-pro` 的模型。以 `deepseek-` 开头的名称会原样转发。其他名称（包括 GPT-5.6 Sol）会改写成 `config.yaml` 里的 `model`。

## 子代理

Cursor 可以同时运行多个子代理。每个子代理有自己的对话和自己的 DeepSeek thinking，与原生的 Claude、GPT、Grok 会话相同。一个子代理不会读取或覆盖另一个子代理的推理。

代理在本地保存 thinking，因为 Cursor 不会把 `reasoning_content` 送回来。正在进行的对话不会被清理删掉。只会删除已经结束的旧对话。

## 出问题时

- **`reasoning_content` must be passed back** — 请求没有经过代理。Base URL 必须是 ngrok 地址且以 `/v1` 结尾，启动器窗口要保持打开。
- **Cursor 无法访问 localhost** — 使用启动器给出的 ngrok 地址，不要用 `127.0.0.1`。
- **上下文不到 1M** — 选择一次 **GPT-5.6 Sol**，让 Cursor 套用目录里的 1M 预算。
- **多个子代理之后出现 Provider error** — 代理会自动重试中断的 DeepSeek 连接。如果还是旧版本，请重启启动器后再发一次消息。

其余选项见 [`config.example.yaml`](config.example.yaml)。

## 许可证

MIT。原始设计和 reasoning 修复：[yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy)。
