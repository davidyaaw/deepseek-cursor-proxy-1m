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

首次运行会创建 `~/.deepseek-cursor-proxy/config.yaml`。`model` 只是未知名称的回退；Sol 和 Terra 有各自的映射：

```yaml
model: deepseek-flash
```

## 连接 Cursor

1. 打开 **Settings → Models → API Keys**
2. 启用 **Override OpenAI Base URL**，粘贴启动器给出的地址，必须以 `/v1` 结尾。
3. 在 **OpenAI API Key** 中填入 DeepSeek 密钥（`sk-...`）。
4. 按下表选择模型和 **Effort**。Sol 和 Terra 使用 Cursor 的 1M 预算，实际由 DeepSeek 回答。每次发送时，Effort 会按模型分别保存。
5. 启动器会自动安装修复，这样在开启 OpenAI 密钥时 Composer 和 Grok 仍可使用。Claude 和 Gemini 本来就不使用这把密钥。其他 Cursor 模型在密钥开启时仍然无法使用。Windows 可能询问一次管理员权限。之后请完全退出并重新打开 Cursor。

<img src="assets/cursor_config.png" width="600" alt="Cursor 中的 API 密钥和 Base URL 设置">

| Cursor | DeepSeek |
| --- | --- |
| GPT-5.6 Sol | `deepseek-v4-pro` |
| GPT-5.6 Terra | `deepseek-flash` |
| 以 `deepseek-` 开头 | 原样转发 |
| 其他名称 | `config.yaml` 里的 `model` |

最后两行只针对经过代理的请求。Composer、Grok、Claude 和 Gemini 不走这里。

以 `deepseek-` 开头的名称会原样发给 DeepSeek：`deepseek-v4-pro` 仍是 `deepseek-v4-pro`，`deepseek-flash` 仍是 `deepseek-flash`。这种情况不用改 `config.yaml`。

除了 Sol 和 Terra，其他名称会被丢掉。代理改用 `~/.deepseek-cursor-proxy/config.yaml` 里的 `model:`。现在的后备模型是 `deepseek-flash`。若要改成 Pro，写成 `model: deepseek-v4-pro`，然后重启启动器。

平时在 Cursor 里选两项即可：**GPT-5.6 Sol** 是 Pro，**GPT-5.6 Terra** 是 Flash。

| Cursor Effort | DeepSeek |
| --- | --- |
| None | 关闭 thinking（`{"thinking": {"type": "disabled"}}`） |
| Low | 开启 thinking，`reasoning_effort: low` |
| Medium、High | 开启 thinking，`reasoning_effort: high` |
| Extra High、Max | 开启 thinking，`reasoning_effort: max` |

**None** 是快速路径，不产生 reasoning token。**High** 适合日常代理任务。**Max** 留给最难的问题。在同一对话里切换模型或 Effort 会丢掉之前的 thinking。下一条回复可能以 `[deepseek-cursor-proxy] Refreshed reasoning_content history.` 开头。之后会保留新的 thinking。

## 子代理

Cursor 可以同时运行多个子代理。每个子代理有自己的对话和自己的 DeepSeek thinking，与原生的 Claude、GPT、Grok 会话相同。一个子代理不会读取或覆盖另一个子代理的推理。

代理在本地保存 thinking，因为 Cursor 不会把 `reasoning_content` 送回来。正在进行的对话不会被清理删掉。只会删除已经结束的旧对话。

## 出问题时

- **`reasoning_content` must be passed back** — 请求没有经过代理。Base URL 必须是 ngrok 地址且以 `/v1` 结尾，启动器窗口要保持打开。
- **Cursor 无法访问 localhost** — 使用启动器给出的 ngrok 地址，不要用 `127.0.0.1`。
- **上下文不到 1M** — 选择一次 **GPT-5.6 Sol** 或 **GPT-5.6 Terra**，让 Cursor 套用目录里的 1M 预算。
- **多个子代理之后出现 Provider error** — 代理会自动重试中断的 DeepSeek 连接。如果还是旧版本，请重启启动器后再发一次消息。

其余选项见 [`config.example.yaml`](config.example.yaml)。

## 许可证

MIT。原始设计和 reasoning 修复：[yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy)。
