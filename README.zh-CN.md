<h1 align="center"><img src="assets/logo.png" width="150" alt="deepseek-cursor-proxy logo"><br>DeepSeek Cursor Proxy &mdash; 1M 上下文分支</h1>

<p align="center"><a href="README.md">English</a> | <a href="README.ru.md">Русский</a> | <b>简体中文</b></p>

一个兼容性代理，将 **Cursor**（以及其他兼容 OpenAI 接口的编程智能体）连接到
**DeepSeek 思考模型**。它修复了工具调用时的 `reasoning_content` 报错，并让你在
Cursor 中以完整的 **1,000,000 token 上下文窗口** 使用 DeepSeek（见
[1M 指南](#在-cursor-中使用-1m-上下文窗口)）。

> **这是一个分支（fork）。** 基于
> [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy)
> （MIT 许可证）。原始设计和核心的 reasoning 修复全部归功于上游作者。本分支新增了
> OpenAI 专有工具过滤以及一键启动脚本——见
> [本分支新增内容](#本分支新增内容)。

---

## 目录

- [在 Cursor 中使用 1M 上下文窗口](#在-cursor-中使用-1m-上下文窗口)
- [本分支新增内容](#本分支新增内容)
- [功能](#功能)
- [环境要求](#环境要求)
- [安装](#安装)
- [第 1 步：配置 ngrok](#第-1-步配置-ngrok)
- [第 2 步：启动代理](#第-2-步启动代理)
- [第 3 步：让 Cursor 指向代理](#第-3-步让-cursor-指向代理)
- [第 4 步：在 Cursor 中与 DeepSeek 对话](#第-4-步在-cursor-中与-deepseek-对话)
- [配置参考](#配置参考)
- [Windows 快速上手](#windows-快速上手)
- [配合其他智能体使用](#配合其他智能体使用)
- [故障排查](#故障排查)
- [工作原理](#工作原理)
- [开发](#开发)
- [致谢与许可证](#致谢与许可证)

---

## 在 Cursor 中使用 1M 上下文窗口

Cursor **不会** 从接口响应中读取上下文窗口大小，而是取自它自己的 **模型目录**。
该目录中没有 1M 档位的 DeepSeek 模型，因此获得 1M 窗口的方法是：让 Cursor
以为自己在和一个拥有 1M 窗口的模型对话，而代理悄悄把请求转发给 DeepSeek。

之所以可行，是因为代理会把 **任何不以 `deepseek-` 开头的模型名** 改写为
`~/.deepseek-cursor-proxy/config.yaml` 中配置的后备模型（`model:` 设置，例如
`deepseek-flash`）。

### 操作步骤

1. 在 Cursor 中启用自定义接口：**Settings &rarr; Models &rarr; API Keys
   &rarr; Override OpenAI Base URL** =
   `https://<你的隧道>.ngrok-free.app/v1`，并在 OpenAI API Key 字段中填入你的
   DeepSeek API 密钥。使用 `Ctrl+Shift+0`（Windows/Linux）或 `Cmd+Shift+0`
   （macOS）切换开关。
2. 在 **Cursor 模型选择器中选择 `GPT-5.6 Sol`**——这是 Cursor 目录中带有
   **1M** 上下文窗口的模型。Cursor 会向代理发送例如 `model: "gpt-5.6-sol"`，
   并按最多 1M token 分配预算。
3. 代理检测到非 DeepSeek 的模型 id，将其改写为你配置的后备模型
   （`config.yaml` 中的 `model:`），于是 **实际由 DeepSeek 作答**——并享有
   Cursor 分配的 1M 预算。
4. 首次运行之后就不必再选 GPT-5.6 Sol：在 Cursor 中选择你自己的
   **`deepseek-flash`** 模型同样以 1M 运行（Cursor 对目录外的 BYOK 模型默认
   给予 1M），代理会将 `deepseek-flash` 原样转发给 DeepSeek。

```text
Cursor 模型选择器:  GPT-5.6 Sol          (Cursor 目录: 1M 窗口)
        |
        |  model = "gpt-5.6-sol"  ->  Override OpenAI Base URL  ->  代理
        v
   代理: 非 DeepSeek id  ->  改写为后备模型 (config.yaml: model)
        |
        v
DeepSeek API  (1M token 窗口)
```

确保 `config.yaml` 中的 `model:` 是你希望非 DeepSeek 请求被改写成的模型：

```yaml
# ~/.deepseek-cursor-proxy/config.yaml
model: deepseek-flash
```

### 注意事项

- 任何 `deepseek-*` 模型 id 都会原样转发给 DeepSeek，因此使用
  `deepseek-flash` 时不会发生改写。
- Cursor 仍会叠加自身套餐的上限，并且按 *假定的* 窗口大小计算自动压缩。如果
  DeepSeek 模型的真实窗口更小，提供方的报错可能先于压缩触发——见
  [故障排查](#故障排查)。

---

## 本分支新增内容

相对上游，本分支包含：

- **丢弃 OpenAI 专有工具条目。** 对于 GPT 命名的模型，Cursor 会发送自由格式的
  `{"type": "custom", ...}` 工具；DeepSeek 会以 `tools[i].type: unknown
  variant custom, expected function` 拒绝整个请求。代理会丢弃这些条目（以及指向
  它们的 `tool_choice`）并记录警告，而不是让请求失败。
- **一键启动脚本。** `Start DeepSeek Proxy.cmd` + `start-deepseek-proxy.ps1`
  启动代理、等待 ngrok 隧道、打印 Cursor Base URL 并复制到剪贴板。另附跨平台的
  `start-deepseek-proxy.sh`。
- **示例配置**（`config.example.yaml`），每个选项都有说明。

---

## 功能

- **注入 `reasoning_content`** 到发出的工具调用请求中。Cursor 不包含该字段，
  因此代理会从普通及流式 DeepSeek 响应中恢复先前缓存的 reasoning。见
  [DeepSeek 思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode#tool-calls)。
- **在 Cursor 中显示 DeepSeek 的思考内容**，以可折叠的 Markdown
  `<details><summary>Thinking</summary>...</details>` 块呈现。
- **启动 ngrok 隧道**，让 Cursor 可以通过公网 HTTPS 地址访问本地代理。
- **改写非 DeepSeek 模型名** 为后备模型——这正是在 Cursor 中选择 1M 目录模型时
  获得 1M 窗口的关键。
- **应用其他兼容性修复**，让 DeepSeek 模型在 Cursor 中运行良好（见
  [本分支新增内容](#本分支新增内容) 和 [工作原理](#工作原理)）。

## 为什么需要它

没有代理时，Cursor + DeepSeek 思考模式在工具调用时会失败：

<img src="assets/error_400.png" width="600" alt="错误 400 - reasoning_content must be passed back">

```txt
⚠️ Connection Error
Provider returned error:
{
  "error": {
    "message": "The reasoning_content in the thinking mode must be passed back to the API.",
    "type": "invalid_request_error",
    "param": null,
    "code": "invalid_request_error"
  }
}
```

---

## 环境要求

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)**（推荐）或 `pip`
- **[ngrok](https://ngrok.com/)**（Cursor 需要，因为它屏蔽 `localhost` API
  地址；如果只配合接受本地地址的智能体使用则可选）
- **DeepSeek API 密钥**（`sk-...`），在
  [platform.deepseek.com](https://platform.deepseek.com/api_keys) 创建

---

## 安装

### 方式 A：uv（推荐）

```bash
# 如果还没有 uv，先安装
curl -LsSf https://astral.sh/uv/install.sh | sh

# 克隆并启动（uv 会在仓库内创建 .venv/）
git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
uv run deepseek-cursor-proxy
```

### 方式 B：pip / conda

```bash
conda create -n dcp python=3.10 -y
conda activate dcp

git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
pip install -e .

# 启动
deepseek-cursor-proxy
```

首次运行时代理会创建：

- `~/.deepseek-cursor-proxy/config.yaml` —— 配置文件
- `~/.deepseek-cursor-proxy/reasoning_content.sqlite3` —— reasoning 缓存

---

## 第 1 步：配置 ngrok

Cursor 会屏蔽 `localhost` 等非公网 API 地址，因此代理需要一个公网 HTTPS 地址。
[ngrok](https://ngrok.com/) 无需在路由器上开放端口即可暴露本地代理。（也可以使用
[Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/setup/)。）

注册 ngrok 账号，然后安装并认证一次：

```bash
brew install ngrok          # macOS；其他平台见 ngrok 文档
ngrok config add-authtoken <你的_NGROK_AUTHTOKEN>
```

如果只在接受 `localhost` 接口的应用中使用代理，可以跳过这一步：在
`~/.deepseek-cursor-proxy/config.yaml` 中设置 `ngrok: false`，或使用
`--no-ngrok` 启动。

## 第 2 步：启动代理

```bash
uv run deepseek-cursor-proxy
```

启用 ngrok 时，代理启动后会打印公网地址：

```text
localhost:9000
api_base_url: https://<你的隧道>.ngrok-free.app/v1
```

如果该地址与 Cursor 中配置的不同，请更新 Cursor 的 Base URL。

**固定 ngrok 地址 / 自定义域名：** 将保留的地址传给 ngrok：

```yaml
# ~/.deepseek-cursor-proxy/config.yaml
ngrok: true
ngrok_url: https://your-subdomain.ngrok.dev
```

```bash
deepseek-cursor-proxy --ngrok-url https://your-subdomain.ngrok.dev
```

## 第 3 步：让 Cursor 指向代理

在 Cursor 中：**Settings &rarr; Models &rarr; API Keys**，然后：

- 启用 **Override OpenAI Base URL**，填入代理地址 **并带上 `/v1`**。
  ```text
  https://<你的隧道>.ngrok-free.app/v1
  ```
- 将 **DeepSeek API 密钥**（`sk-...`）粘贴到 OpenAI API Key 字段。代理只负责
  转发，密钥不会保存在仓库或缓存中。
- **要使用 1M 窗口：** 在模型选择器中选择 Cursor 内置的 **`GPT-5.6 Sol`**——
  代理会把它改写为 `config.yaml` 中的 DeepSeek 后备模型（见
  [1M 指南](#在-cursor-中使用-1m-上下文窗口)）。
- 或者在 **Model Names** 中直接添加 DeepSeek 模型：

  ```text
  deepseek-flash
  deepseek-v4-pro
  ```

<img src="assets/cursor_config.png" width="600" alt="通过代理使用 DeepSeek 的 Cursor 设置">

切换自定义 API 开关：

- macOS：`Cmd+Shift+0`
- Windows/Linux：`Ctrl+Shift+0`

## 第 4 步：在 Cursor 中与 DeepSeek 对话

在 Cursor 中选择 **`GPT-5.6 Sol`**（获得 1M 窗口）或你的 `deepseek-flash`
模型，像平常一样使用聊天或智能体模式。思考内容会出现在可折叠的 **Thinking** 块中。

<img src="assets/cursor_chat.png" width="480" alt="通过代理与 DeepSeek 对话">

---

## 配置参考

持久化设置保存在 `~/.deepseek-cursor-proxy/config.yaml`。带注释的示例见
[`config.example.yaml`](config.example.yaml)。每个选项都可以用命令行参数覆盖。

| `config.yaml` 键 | CLI 参数 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `host` | `--host` | `127.0.0.1` | 绑定地址 |
| `port` | `--port` | `9000` | 绑定端口 |
| `base_url` | `--base-url` | `https://api.deepseek.com` | DeepSeek API 基础 URL |
| `model` | `--model` | `deepseek-v4-pro` | 非 DeepSeek id（如 GPT-5.6 Sol）的后备 **及改写目标** |
| `thinking` | `--thinking` | `enabled` | `enabled` / `disabled` |
| `reasoning_effort` | `--reasoning-effort` | `max` | `low` \| `medium` \| `high` \| `max` \| `xhigh` |
| `display_reasoning` | `--display-reasoning` | `true` | 在 Cursor 内容中显示思考过程 |
| `collasible_reasoning` | `--collapsible-reasoning` | `true` | 折叠思考块 |
| `ngrok` | `--ngrok` / `--no-ngrok` | `true` | 启动 ngrok 隧道 |
| `ngrok_url` | `--ngrok-url` | &mdash; | 固定/保留的 ngrok 地址 |
| `verbose` | `--verbose` | `false` | 记录完整请求元数据和负载 |
| `request_timeout` | `--request-timeout` | `300` | 上游超时（秒） |
| `max_request_body_bytes` | `--max-request-body-bytes` | `20971520` | 最大请求体大小 |
| `cors` | `--cors` / `--no-cors` | `false` | 发送宽松的 CORS 头 |
| `reasoning_content_path` | `--reasoning-content-path` | `reasoning_content.sqlite3` | reasoning 缓存路径 |
| `missing_reasoning_strategy` | `--missing-reasoning-strategy` | `recover` | `recover`（宽松）或 `reject`（严格） |
| `reasoning_cache_max_age_seconds` | `--reasoning-cache-max-age-seconds` | `2592000` | 缓存条目最长保留时间（30 天） |
| `reasoning_cache_max_rows` | `--reasoning-cache-max-rows` | `100000` | 缓存条目上限 |

其他参数：`--config <路径>`（使用其他配置文件）、`--trace-dir <目录>`（写入完整
结构化追踪）、`--clear-reasoning-cache`（清空缓存后退出）。历史拼写错误
`collasible_reasoning` 与 `collapsible_reasoning` 均可使用。

常用示例：

```bash
# 在 Cursor 界面中隐藏思考内容
deepseek-cursor-proxy --no-display-reasoning

# 完整详细日志
deepseek-cursor-proxy --verbose

# 仅 localhost（适用于接受本地地址的智能体）
deepseek-cursor-proxy --no-ngrok --port 9000

# 修改非 DeepSeek id（如 GPT-5.6 Sol）被改写成的模型
deepseek-cursor-proxy --model deepseek-flash

# 清空本地 reasoning 缓存
deepseek-cursor-proxy --clear-reasoning-cache
```

---

## Windows 快速上手

1. 安装 Python、`uv` 和 `ngrok`（认证一次 ngrok，见
   [第 1 步](#第-1-步配置-ngrok)）。
2. 双击仓库根目录下的 **`Start DeepSeek Proxy.cmd`**。

启动脚本会启动代理和 ngrok 隧道，等待公网地址，打印并复制到剪贴板：

```text
================================================================
 DeepSeek proxy is READY
================================================================

 Cursor Base URL (already copied to clipboard):

 https://<你的隧道>.ngrok-free.app/v1

 Paste into: Settings -> Models -> API Keys -> Override OpenAI Base URL
 For 1M context: select GPT-5.6 Sol (or your deepseek-flash)
 Toggle custom API:  Ctrl+Shift+0
```

在 Cursor 中工作时保持窗口打开；关闭窗口（或按 `Ctrl+C`）即可停止代理。

启动脚本默认使用自身所在目录，无需配置路径。如需指定其他目录，传入
`-ProxyDir <路径>`：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-deepseek-proxy.ps1 -ProxyDir 'C:\path\to\deepseek-cursor-proxy-1m'
```

在 macOS/Linux 上使用 shell 启动脚本：

```bash
./start-deepseek-proxy.sh
```

---

## 配合其他智能体使用

将任何兼容 OpenAI 的客户端的 base URL 指向代理即可。接受本地接口的智能体可以直接
使用 `http://127.0.0.1:9000/v1`（`ngrok: false`）。任何不以 `deepseek-` 开头的
模型 id 都会被改写为后备模型，适合只允许目录内模型名的客户端。

代理提供的接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat completions（唯一支持的 POST 路径） |
| `GET` | `/v1/models` | 模型列表 |
| `GET` | `/v1/healthz` | 健康检查 |

---

## 故障排查

- **`The reasoning_content in the thinking mode must be passed back`** ——
  确认请求确实经过代理（Base URL 以 `/v1` 结尾），并让一次正常对话完成以便缓存
  reasoning。如果缓存缺少条目，终端会打印 `context status=missing`。
- **`tools[i].type: unknown variant custom`** —— 本分支已处理：此类工具会被
  丢弃并记录警告。如果仍然出现，请更新到本分支。
- **Cursor 中上下文不足 1M** —— 按照 [1M 指南](#在-cursor-中使用-1m-上下文窗口)
  选择 **`GPT-5.6 Sol`**。同时检查 `config.yaml` 中的 `model:` 是否为预期的
  DeepSeek 模型。
- **自动压缩触发前出现提供方报错** —— Cursor 按假定窗口（1M）计算压缩。如果
  DeepSeek 模型的真实窗口更小，先切换到 Auto 模式让压缩执行，再切回来。
- **Cursor 无法访问代理** —— Cursor 屏蔽 `localhost`，请使用 ngrok 地址。确认
  ngrok 隧道已启动且带有 `/v1` 后缀。
- **`Certificate verify failed`** —— 更新 ngrok 并重新执行
  `ngrok config add-authtoken`。

---

## 工作原理

- **核心修复。** DeepSeek
  [思考模式下的工具调用](https://api-docs.deepseek.com/guides/thinking_mode#tool-calls)
  要求在后续请求中回传完整的 **多轮** `reasoning_content` 链。Cursor 省略了该
  字段，导致 400。代理（`Cursor -> ngrok -> 代理 -> DeepSeek API`）保存 DeepSeek
  原始的 `reasoning_content`，并将缺失的部分补回到发出的工具调用历史中。
- **非 DeepSeek 模型改写。** 任何不以 `deepseek-` 开头的模型 id 都会被改写为
  后备模型（`config.yaml` 中的 `model:`）。结合 Cursor 的模型目录即可获得 1M
  窗口——见 [在 Cursor 中使用 1M 上下文窗口](#在-cursor-中使用-1m-上下文窗口)。
- **多会话隔离。** 缓存键由规范化会话前缀（角色、内容和工具调用，不含
  `reasoning_content`）的 SHA-256 哈希，加上上游模型、配置和 API 密钥哈希共同
  确定。不同会话拥有不同作用域，因此重复的工具调用 ID 不会冲突；字节完全相同的
  历史会得到相同的作用域。
- **兼容上下文缓存。** 代理从不注入合成的会话 ID、时间戳或缓存控制消息，并且按
  原样恢复 `reasoning_content`，从而使重复前缀对
  [DeepSeek 上下文缓存](https://api-docs.deepseek.com/guides/kv_cache)
  保持不变。缓存命中率会记录在日志中。
- **其他兼容性修复。** 旧版 `functions`/`function_call` 字段转换为
  `tools`/`tool_choice`；保留 required 和指定工具的选择语义；规范化
  `reasoning_effort` 别名；丢弃 OpenAI 专有的 `tools[].type: "custom"` 条目；
  从助手内容中剥离镜像的思考显示块；将多段 content 数组展平为纯文本；并将
  `reasoning_content` 镜像到 Cursor 可见的 Markdown details 块中。

---

## 开发

运行单元测试：

```bash
uv run python -m unittest discover -s tests
```

运行 pre-commit 钩子（格式化与 lint）：

```bash
uv sync --dev
uv run pre-commit run --all-files
```

调试：

```bash
# 详细输出
deepseek-cursor-proxy --verbose

# 仅 localhost、详细输出，便于 curl 测试
deepseek-cursor-proxy --no-ngrok --port 9000 --verbose

# 捕获完整的结构化请求追踪
deepseek-cursor-proxy --verbose --trace-dir ./trace-dumps

# 使用其他配置文件
deepseek-cursor-proxy --config ./dev.config.yaml
```

---

## 致谢与许可证

本项目是
[yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy)
的分支，基于 [MIT 许可证](LICENSE) 发布。原始版权 &copy; Yixing Lao。感谢上游作者
和贡献者提供的核心代理及 reasoning 修复设计。
