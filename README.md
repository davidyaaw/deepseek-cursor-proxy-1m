<!-- <h1><img src="assets/logo.png" width="120" alt="deepseek-cursor-proxy logo" style="vertical-align: middle;">&nbsp;DeepSeek Cursor Proxy (1M)</h1> -->
<h1 align="center"><img src="assets/logo.png" width="150" alt="deepseek-cursor-proxy logo"><br>DeepSeek Cursor Proxy &mdash; 1M context fork</h1>

<p align="center"><b>English</b> | <a href="README.ru.md">Русский</a> | <a href="README.zh-CN.md">简体中文</a></p>

A compatibility proxy that connects **Cursor** (and other OpenAI-compatible
coding agents) to **DeepSeek thinking models**. It fixes the
`reasoning_content` tool-call error and lets you run DeepSeek inside Cursor with
Cursor's full **1,000,000-token context window** (see
[the 1M guide](#the-1m-token-context-window-in-cursor)).

> **This is a fork.** It is based on
> [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy)
> (MIT licensed). All credit for the original design and the core reasoning
> repair goes to the upstream author. This fork adds OpenAI-only tool filtering
> and one-click launchers — see
> [What this fork adds](#what-this-fork-adds).

---

## Table of contents

- [The 1M-token context window in Cursor](#the-1m-token-context-window-in-cursor)
- [What this fork adds](#what-this-fork-adds)
- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Installation](#installation)
- [Step 1 &mdash; Set up ngrok](#step-1--set-up-ngrok)
- [Step 2 &mdash; Start the proxy](#step-2--start-the-proxy)
- [Step 3 &mdash; Point Cursor at the proxy](#step-3--point-cursor-at-the-proxy)
- [Step 4 &mdash; Chat with DeepSeek in Cursor](#step-4--chat-with-deepseek-in-cursor)
- [Configuration reference](#configuration-reference)
- [Windows quick start](#windows-quick-start)
- [Using the proxy with other agents](#using-the-proxy-with-other-agents)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Development](#development)
- [Credits and license](#credits-and-license)

---

## The 1M-token context window in Cursor

Cursor does **not** read the context window from the endpoint's response — it
takes it from its own **model catalog**. DeepSeek models are not in that catalog
at a 1M tier, so the way to get a 1M window is to let Cursor believe it is
talking to a model that *does* have one, while the proxy silently redirects the
request to DeepSeek.

That works because the proxy rewrites **any non-`deepseek-*` model name** to the
fallback model configured in `~/.deepseek-cursor-proxy/config.yaml`
(the `model:` setting, e.g. `deepseek-flash`).

### Recipe

1. In Cursor enable the custom endpoint: **Settings &rarr; Models &rarr; API
   Keys &rarr; Override OpenAI Base URL** =
   `https://<your-tunnel>.ngrok-free.app/v1`, and put your DeepSeek API key in
   the OpenAI API Key field. Toggle it on/off with `Ctrl+Shift+0`
   (Windows/Linux) or `Cmd+Shift+0` (macOS).
2. In the **Cursor model picker select `GPT-5.6 Sol`** — a Cursor catalog model
   that carries a **1M** context window. Cursor now sends e.g.
   `model: "gpt-5.6-sol"` to your proxy and budgets up to 1M tokens.
3. The proxy sees a non-DeepSeek id and rewrites it to your configured fallback
   (`model:` in `config.yaml`), so **DeepSeek actually answers** the request —
   with the 1M budget Cursor granted.
4. After that first launch you no longer have to pick GPT-5.6 Sol: selecting
   your own **`deepseek-flash`** model in Cursor also runs at 1M (Cursor gives
   non-catalog BYOK models the 1M default), and the proxy forwards
   `deepseek-flash` to DeepSeek unchanged.

```text
Cursor model picker:  GPT-5.6 Sol          (Cursor catalog: 1M window)
        |
        |  model = "gpt-5.6-sol"  ->  Override OpenAI Base URL  ->  proxy
        v
   proxy: non-DeepSeek id  ->  rewrite to fallback (config.yaml: model)
        |
        v
DeepSeek API  (1M-token window)
```

Make sure `model:` in `config.yaml` is the DeepSeek model you want non-DeepSeek
requests to be rewritten to:

```yaml
# ~/.deepseek-cursor-proxy/config.yaml
model: deepseek-flash
```

### Notes

- Any `deepseek-*` model id is forwarded to DeepSeek as-is, so once you are
  using `deepseek-flash` nothing is rewritten.
- Cursor still enforces its own per-plan ceiling on top of this, and it computes
  auto-compaction against the *assumed* window. If DeepSeek's real window for
  your model is smaller than assumed, the provider error can arrive before
  compaction triggers — see [Troubleshooting](#troubleshooting).

---

## What this fork adds

Relative to upstream, this fork contains:

- **Dropping of OpenAI-only tool entries.** Cursor sends free-form
  `{"type": "custom", ...}` tools for GPT-named models; DeepSeek rejects the
  whole request with `tools[i].type: unknown variant custom, expected
  function`. The proxy drops those entries (and any `tool_choice` that pointed
  at them) and logs a warning instead of failing.
- **One-click launchers.** `Start DeepSeek Proxy.cmd` + `start-deepseek-proxy.ps1`
  start the proxy, wait for the ngrok tunnel, print the Cursor Base URL and copy
  it to the clipboard. A cross-platform `start-deepseek-proxy.sh` is included
  too.
- **Example configuration** (`config.example.yaml`) with every option
  documented.

---

## What it does

- **Injects `reasoning_content`** into outgoing tool-call requests. Cursor does
  not include the field, so the proxy restores previously cached reasoning from
  regular and streamed DeepSeek responses. See the
  [DeepSeek thinking-mode docs](https://api-docs.deepseek.com/guides/thinking_mode#tool-calls).
- **Displays DeepSeek's thinking tokens in Cursor** by forwarding them into
  collapsible Markdown `<details><summary>Thinking</summary>...</details>`
  blocks.
- **Starts an ngrok tunnel** so Cursor can reach the local proxy through a public
  HTTPS URL.
- **Rewrites non-DeepSeek model names** to the configured fallback — this is
  what gives you a 1M window when you pick a 1M catalog model in Cursor.
- **Applies other compatibility fixes** to make DeepSeek models run well in
  Cursor (see [What this fork adds](#what-this-fork-adds) and
  [How it works](#how-it-works)).

## Why this exists

Without the proxy, Cursor + DeepSeek in thinking mode fails on tool calls:

<img src="assets/error_400.png" width="600" alt="Error 400 - reasoning_content must be passed back">

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

## Requirements

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** (recommended) or `pip`
- **[ngrok](https://ngrok.com/)** (needed for Cursor, which blocks `localhost`
  API URLs — optional if you only use the proxy with agents that accept a local
  base URL)
- A **DeepSeek API key** (`sk-...`), created at
  [platform.deepseek.com](https://platform.deepseek.com/api_keys)

---

## Installation

### Option A &mdash; uv (recommended)

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and start (uv creates .venv/ inside the repo)
git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
uv run deepseek-cursor-proxy
```

### Option B &mdash; pip / conda

```bash
conda create -n dcp python=3.10 -y
conda activate dcp

git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
pip install -e .

# Start
deepseek-cursor-proxy
```

On the first run the proxy creates:

- `~/.deepseek-cursor-proxy/config.yaml` — the configuration file
- `~/.deepseek-cursor-proxy/reasoning_content.sqlite3` — the reasoning cache

---

## Step 1 &mdash; Set up ngrok

Cursor blocks non-public API URLs such as `localhost`, so the proxy needs a
public HTTPS URL. [ngrok](https://ngrok.com/) exposes the local proxy without
opening router ports. (You can also use
[Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/setup/).)

Create an ngrok account, then install and authenticate ngrok once:

```bash
brew install ngrok          # macOS; see ngrok docs for other platforms
ngrok config add-authtoken <YOUR_NGROK_AUTHTOKEN>
```

If you use the proxy only with an application that accepts `localhost` endpoints,
skip this step by setting `ngrok: false` in `~/.deepseek-cursor-proxy/config.yaml`
or by starting with `--no-ngrok`.

## Step 2 &mdash; Start the proxy

```bash
uv run deepseek-cursor-proxy
```

When ngrok is enabled the proxy prints the public URL on start:

```text
localhost:9000
api_base_url: https://<your-tunnel>.ngrok-free.app/v1
```

If the URL differs from the one configured in Cursor, update Cursor's Base URL.

**Fixed ngrok endpoint / custom domain:** pass the reserved URL through to the
ngrok agent:

```yaml
# ~/.deepseek-cursor-proxy/config.yaml
ngrok: true
ngrok_url: https://your-subdomain.ngrok.dev
```

```bash
deepseek-cursor-proxy --ngrok-url https://your-subdomain.ngrok.dev
```

## Step 3 &mdash; Point Cursor at the proxy

In Cursor: **Settings &rarr; Models &rarr; API Keys**, then:

- Enable **Override OpenAI Base URL** and enter your proxy URL **with `/v1`**.
  ```text
  https://<your-tunnel>.ngrok-free.app/v1
  ```
- Paste your **DeepSeek API key** (`sk-...`) into the OpenAI API Key field. The
  proxy forwards it upstream; it is never stored in the repo or in the cache.
- **For the 1M-token window:** pick the Cursor-built-in **`GPT-5.6 Sol`** model
  in the model selector — the proxy rewrites it to the DeepSeek fallback from
  `config.yaml` (see
  [the 1M guide](#the-1m-token-context-window-in-cursor)).
- Otherwise, under **Model Names**, add the DeepSeek model directly:

  ```text
  deepseek-flash
  deepseek-v4-pro
  ```

<img src="assets/cursor_config.png" width="600" alt="Cursor settings for DeepSeek through the proxy">

Toggle the custom API on/off with:

- macOS: `Cmd+Shift+0`
- Windows/Linux: `Ctrl+Shift+0`

## Step 4 &mdash; Chat with DeepSeek in Cursor

Select **`GPT-5.6 Sol`** (for the 1M window) or your `deepseek-flash` model in
Cursor and use chat or agent mode as usual. Thinking tokens appear in a
collapsible **Thinking** block.

<img src="assets/cursor_chat.png" width="480" alt="Chatting with DeepSeek through the proxy">

---

## Configuration reference

Persistent settings live in `~/.deepseek-cursor-proxy/config.yaml`. See
[`config.example.yaml`](config.example.yaml) for a documented copy. Every option
can also be overridden with a command-line flag.

| `config.yaml` key | CLI flag | Default | Meaning |
| --- | --- | --- | --- |
| `host` | `--host` | `127.0.0.1` | Bind address |
| `port` | `--port` | `9000` | Bind port |
| `base_url` | `--base-url` | `https://api.deepseek.com` | DeepSeek API base URL |
| `model` | `--model` | `deepseek-v4-pro` | Fallback **and rewrite target** for non-DeepSeek ids (e.g. GPT-5.6 Sol) |
| `thinking` | `--thinking` | `enabled` | `enabled` / `disabled` |
| `reasoning_effort` | `--reasoning-effort` | `max` | `low` \| `medium` \| `high` \| `max` \| `xhigh` |
| `display_reasoning` | `--display-reasoning` | `true` | Mirror thinking into Cursor content |
| `collasible_reasoning` | `--collapsible-reasoning` | `true` | Collapse the mirrored thinking block |
| `ngrok` | `--ngrok` / `--no-ngrok` | `true` | Start an ngrok tunnel |
| `ngrok_url` | `--ngrok-url` | &mdash; | Fixed/reserved ngrok endpoint |
| `verbose` | `--verbose` | `false` | Log full request metadata and payloads |
| `request_timeout` | `--request-timeout` | `300` | Upstream timeout (seconds) |
| `max_request_body_bytes` | `--max-request-body-bytes` | `20971520` | Max accepted request size |
| `cors` | `--cors` / `--no-cors` | `false` | Send permissive CORS headers |
| `reasoning_content_path` | `--reasoning-content-path` | `reasoning_content.sqlite3` | Reasoning cache path |
| `missing_reasoning_strategy` | `--missing-reasoning-strategy` | `recover` | `recover` (friendly) or `reject` (strict) |
| `reasoning_cache_max_age_seconds` | `--reasoning-cache-max-age-seconds` | `2592000` | Cache row max age (30 days) |
| `reasoning_cache_max_rows` | `--reasoning-cache-max-rows` | `100000` | Cache row cap |

Other flags: `--config <path>` (use another config file), `--trace-dir <dir>`
(write full structured traces), `--clear-reasoning-cache` (wipe the cache and
exit). The historical typo `collasible_reasoning` is accepted alongside
`collapsible_reasoning`.

Useful examples:

```bash
# Hide thinking tokens in the Cursor UI
deepseek-cursor-proxy --no-display-reasoning

# Run with full verbose logging
deepseek-cursor-proxy --verbose

# Run localhost-only (for agents that accept local URLs)
deepseek-cursor-proxy --no-ngrok --port 9000

# Change the model that non-DeepSeek ids (e.g. GPT-5.6 Sol) are rewritten to
deepseek-cursor-proxy --model deepseek-flash

# Clear the local reasoning cache
deepseek-cursor-proxy --clear-reasoning-cache
```

---

## Windows quick start

1. Install Python, `uv`, and `ngrok` (authenticate ngrok once, see
   [Step 1](#step-1--set-up-ngrok)).
2. Double-click **`Start DeepSeek Proxy.cmd`** in the repository root.

The launcher starts the proxy and its ngrok tunnel, waits for the public URL,
prints it, and copies it to the clipboard:

```text
================================================================
 DeepSeek proxy is READY
================================================================

 Cursor Base URL (already copied to clipboard):

 https://<your-tunnel>.ngrok-free.app/v1

 Paste into: Settings -> Models -> API Keys -> Override OpenAI Base URL
 For 1M context: select GPT-5.6 Sol (or your deepseek-flash)
 Toggle custom API:  Ctrl+Shift+0
```

Keep the window open while you work in Cursor; close it (or press `Ctrl+C`) to
stop the proxy.

The launcher defaults to the folder it lives in, so no path configuration is
needed. To point it elsewhere, pass `-ProxyDir <path>`:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-deepseek-proxy.ps1 -ProxyDir 'C:\path\to\deepseek-cursor-proxy-1m'
```

On macOS/Linux use the shell launcher:

```bash
./start-deepseek-proxy.sh
```

---

## Using the proxy with other agents

Point any OpenAI-compatible client's base URL at the proxy. Agents that accept a
local endpoint can use `http://127.0.0.1:9000/v1` directly
(`ngrok: false`). Any model id that is not `deepseek-*` is rewritten to the
configured fallback, which is handy for clients that only allow catalog model
names.

Endpoints exposed by the proxy:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat completions (the only supported POST path) |
| `GET` | `/v1/models` | Advertised model list |
| `GET` | `/v1/healthz` | Health check |

---

## Troubleshooting

- **`The reasoning_content in the thinking mode must be passed back`** — make
  sure the request actually went through the proxy (check the Base URL ends in
  `/v1`) and let a normal turn complete so the reasoning is cached. If the cache
  is missing rows, the terminal prints `context status=missing`.
- **`tools[i].type: unknown variant custom`** — handled in this fork: such tools
  are dropped with a warning. Update to this fork if you still see it.
- **Context is capped below 1M in Cursor** — pick **`GPT-5.6 Sol`** as described
  in [the 1M guide](#the-1m-token-context-window-in-cursor). Also check that
  `model:` in `config.yaml` is the DeepSeek model you expect non-DeepSeek
  requests to be rewritten to.
- **Provider error before auto-compaction kicks in** — Cursor computes
  compaction against the assumed window (1M). If DeepSeek's real window for your
  model is smaller, do a round trip through Auto mode to let compaction run, then
  switch back.
- **Cursor cannot reach the proxy** — Cursor blocks `localhost`; use the ngrok
  URL. Make sure the ngrok tunnel is up and the `/v1` suffix is present.
- **`Certificate verify failed`** — update ngrok and re-run
  `ngrok config add-authtoken`.

---

## How it works

- **Core fix.** DeepSeek
  [thinking-mode tool calls](https://api-docs.deepseek.com/guides/thinking_mode#tool-calls)
  require the complete **multi-round** `reasoning_content` chain to be sent back
  in later requests. Cursor omits that field, causing a 400. The proxy
  (`Cursor -> ngrok -> proxy -> DeepSeek API`) stores DeepSeek's original
  `reasoning_content` and patches missing blocks back into outgoing tool-call
  history.
- **Non-DeepSeek model rewriting.** Any model id that does not start with
  `deepseek-` is rewritten to the configured fallback (`model:` in
  `config.yaml`). Combined with Cursor's catalog that is what gives you a 1M
  window — see
  [The 1M-token context window in Cursor](#the-1m-token-context-window-in-cursor).
- **Multi-conversation isolation.** Cache keys are scoped by a SHA-256 hash of
  the canonical conversation prefix (roles, content, and tool calls, excluding
  `reasoning_content`) plus the upstream model, configuration, and an API-key
  hash. Different threads get different scopes, so reused tool-call IDs do not
  collide. Byte-identical cloned histories produce identical scopes.
- **Context caching compatibility.** The proxy never injects synthetic thread
  IDs, timestamps, or cache-control messages, and it restores
  `reasoning_content` as the exact original string, so repeated prefixes stay
  intact for the
  [DeepSeek context cache](https://api-docs.deepseek.com/guides/kv_cache). Cache
  hit rates are logged.
- **Additional compatibility fixes.** Legacy `functions`/`function_call` fields
  are converted to `tools`/`tool_choice`; required and named tool-choice
  semantics are preserved; `reasoning_effort` aliases are normalized; OpenAI-only
  `tools[].type: "custom"` entries are dropped; mirrored thinking display blocks
  are stripped from assistant content; multi-part content arrays are flattened to
  plain text; and `reasoning_content` is mirrored into Cursor-visible Markdown
  details blocks.

---

## Development

Run unit tests:

```bash
uv run python -m unittest discover -s tests
```

Run pre-commit hooks (formatting and linting):

```bash
uv sync --dev
uv run pre-commit run --all-files
```

Debugging:

```bash
# Verbose output
deepseek-cursor-proxy --verbose

# Localhost only, verbose, for curl testing
deepseek-cursor-proxy --no-ngrok --port 9000 --verbose

# Capture full structured request traces
deepseek-cursor-proxy --verbose --trace-dir ./trace-dumps

# Use another config file
deepseek-cursor-proxy --config ./dev.config.yaml
```

---

## Credits and license

This project is a fork of
[yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy),
released under the [MIT License](LICENSE). Original copyright &copy; Yixing Lao.
Thank you to the upstream author and contributors for the core proxy and the
reasoning-repair design.
