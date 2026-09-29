<h1 align="center"><img src="assets/logo.png" width="150" alt="deepseek-cursor-proxy logo"><br>DeepSeek Cursor Proxy</h1>

<p align="center"><a href="README.md">English</a> | <a href="README.ru.md">Русский</a> | <b>简体中文</b></p>

在 **Windows** 上的 Cursor 里使用 DeepSeek，上下文窗口为 **1,000,000 token**。并行 **子代理各自保留对话和 thinking**，不再共用同一份推理缓存。

这是 [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy) 的分支（MIT）。原项目修复工具调用时的 `reasoning_content` 错误。本分支增加 1M 窗口、Cursor 子代理隔离，以及 Windows 启动器。

## 安装

仅限 Windows。需要 [uv](https://docs.astral.sh/uv/)、[ngrok](https://ngrok.com/)（执行一次 `ngrok config add-authtoken`）和 [DeepSeek API 密钥](https://platform.deepseek.com/api_keys)。

克隆本仓库，双击 `Start DeepSeek Proxy.cmd`。窗口保持打开。启动器会打印 Base URL 并复制到剪贴板。

在该窗口输入 `help`、`settings`、`status`、`clear` 或 `quit`。`settings verbose on` 无需重启即可打开完整日志。`clear` 会删除本地 thinking 缓存。关闭窗口即停止代理。再次启动会替换上次关闭窗口后留下的进程。

```bat
git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
```

首次运行会创建 `%USERPROFILE%\.deepseek-cursor-proxy\config.yaml`。`model` 只是未知名称的回退。Sol 和 Terra 有各自的映射：

```yaml
model: deepseek-flash
```

## 连接 Cursor

1. 打开 **Settings → Models → API Keys**
2. 启用 **Override OpenAI Base URL**，粘贴启动器给出的地址，必须以 `/v1` 结尾。
3. 在 **OpenAI API Key** 中填入 DeepSeek 密钥（`sk-...`）。
4. 按下表选择模型和 **Effort**。Sol 和 Terra 使用 Cursor 的 1M 预算，实际由 DeepSeek 回答。
5. 启动器会修补 Cursor，使 **Claude、Gemini、Composer 和 Grok** 继续走 Cursor 套餐。DeepSeek 密钥不会随这些模型发送，它们也不经过代理。**Sol、Terra、以 `deepseek-` 开头的名称，以及 Cursor 发到 OpenAI Base URL 的其他模型** 都经过代理。未知名称不会被拒绝：由配置文件里的 `model` 对应的 DeepSeek 回答。Windows 可能询问一次管理员权限。之后请完全退出并重新打开 Cursor。
6. Cursor 询问时允许用户级 Effort 钩子。启动器会把它装到 `~/.cursor/hooks.json`，因此在任何工作区都会运行，不只限于本仓库。每个模型各自保存自己的 Effort。配置文件里的 `reasoning_effort` 仅在该模型还没有已存 Effort 时作为后备。

<img src="assets/cursor_config.png" width="600" alt="Cursor 中的 API 密钥和 Base URL 设置">

| Cursor | DeepSeek |
| --- | --- |
| GPT-5.6 Sol | `deepseek-v4-pro` |
| GPT-5.6 Terra | `deepseek-flash` |
| 以 `deepseek-` 开头 | 原样转发 |
| 其他名称 | `config.yaml` 里的 `model` |

最后两行只针对经过代理的请求。Composer、Grok、Claude 和 Gemini 不走这里。

以 `deepseek-` 开头的名称会原样发给 DeepSeek：`deepseek-v4-pro` 仍是 `deepseek-v4-pro`，`deepseek-flash` 仍是 `deepseek-flash`。这种情况不用改 `config.yaml`。

除了 Sol 和 Terra，其他名称会被丢掉。代理改用配置文件里的 `model:`。现在的后备模型是 `deepseek-flash`。若要改成 Pro，写成 `model: deepseek-v4-pro`，然后重启启动器。

平时在 Cursor 里选两项即可：**GPT-5.6 Sol** 是 Pro，**GPT-5.6 Terra** 是 Flash。

| Cursor Effort | DeepSeek |
| --- | --- |
| None | 关闭 thinking（`{"thinking": {"type": "disabled"}}`） |
| Low | 开启 thinking，`reasoning_effort: low` |
| Medium、High | 开启 thinking，`reasoning_effort: high` |
| Extra High、Max | 开启 thinking，`reasoning_effort: max` |

**None** 是快速路径，不产生 reasoning token。**High** 适合日常代理任务。**Max** 留给最难的问题。切换模型或 Effort 会开始新的 thinking。如果旧的工具回合没有保存过 reasoning，对话会保留。如果只命中一部分，较早的尾部仍可能被丢掉，下一条回复可能以 `[deepseek-cursor-proxy] Refreshed reasoning_content history.` 开头。

## 子代理

Cursor 可以同时运行多个子代理。每个子代理有自己的对话和自己的 DeepSeek thinking。一个子代理不会读取或覆盖另一个子代理的推理。

代理在本地保存 thinking，因为 Cursor 不会把 `reasoning_content` 送回来。正在进行的对话不会被清理删掉。只会删除已经结束的旧对话。

## 出问题时

- **`reasoning_content` must be passed back** — 请求没有经过代理。Base URL 必须是 ngrok 地址且以 `/v1` 结尾，启动器窗口要保持打开。
- **Cursor 无法访问 localhost** — 使用启动器给出的 ngrok 地址，不要用 `127.0.0.1`。
- **上下文不到 1M** — 选择一次 **GPT-5.6 Sol** 或 **GPT-5.6 Terra**，让 Cursor 套用目录里的 1M 预算。
- **Effort 一直是配置文件里的值** — Cursor 询问时允许 `~/.cursor/hooks.json` 里的用户钩子，然后再发一次。启动器会安装这个钩子。
- **多个子代理之后出现 Provider error** — 代理会自动重试中断的 DeepSeek 连接。如果仍然失败，请重启启动器后再发一次消息。

其余选项见 [`config.example.yaml`](config.example.yaml)。

## 许可证

MIT。原始设计和 reasoning 修复：[yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy)。
