<h1 align="center"><img src="assets/logo.png" width="150" alt="deepseek-cursor-proxy logo"><br>DeepSeek Cursor Proxy</h1>

<p align="center"><b>English</b> | <a href="README.ru.md">Русский</a> | <a href="README.zh-CN.md">简体中文</a></p>

Use **DeepSeek** inside **Cursor** with a **1,000,000-token** context window.
Parallel **sub-agents keep their own chat and their own thinking**. They no longer
share one reasoning cache.

This is a fork of [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy) (MIT). The original proxy repairs DeepSeek `reasoning_content` on tool calls. This fork adds the 1M model rewrite, Cursor sub-agent isolation, and one-click launchers.

## Install

You need [uv](https://docs.astral.sh/uv/), [ngrok](https://ngrok.com/) (one-time `ngrok config add-authtoken`), and a [DeepSeek API key](https://platform.deepseek.com/api_keys).

**Windows.** Clone this repo and double-click `Start DeepSeek Proxy.cmd`. Leave the window open. It prints the Base URL and copies it to the clipboard.

**macOS / Linux.**

```bash
git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
./start-deepseek-proxy.sh
```

The first run creates `~/.deepseek-cursor-proxy/config.yaml`. `model` is only the fallback for unknown names; Sol and Terra have their own mapping:

```yaml
model: deepseek-flash
```

## Connect Cursor

1. **Settings → Models → API Keys**
2. Turn on **Override OpenAI Base URL** and paste the URL from the launcher. It must end in `/v1`.
3. Put your DeepSeek key (`sk-...`) in **OpenAI API Key**.
4. Pick a model from the map below. Sol and Terra get Cursor's 1M budget; the proxy answers with DeepSeek. Cursor **Effort** is saved on each send and forwarded as DeepSeek `reasoning_effort` (Medium becomes `high`, Extra High becomes `max`).
5. The launcher installs a Cursor fix so Composer and Grok keep working while the OpenAI key stays on. Claude and Gemini already ignore that key. Other Cursor models still fail with it on. Windows may ask for administrator permission once. Quit Cursor and open it again after that.

<img src="assets/cursor_config.png" width="600" alt="Cursor API key and base URL settings">

| Cursor | DeepSeek |
| --- | --- |
| GPT-5.6 Sol | `deepseek-v4-pro` |
| GPT-5.6 Terra | `deepseek-flash` |
| name starting with `deepseek-` | same name |
| any other name | `model` in `config.yaml` |

The last two rows apply only to requests that go through the proxy. Composer, Grok, Claude, and Gemini do not.

A name that starts with `deepseek-` is sent to DeepSeek unchanged: `deepseek-v4-pro` stays `deepseek-v4-pro`, and `deepseek-flash` stays `deepseek-flash`. You do not edit `config.yaml` for that.

Any other name, besides Sol and Terra, is dropped. The proxy uses the `model:` line in `~/.deepseek-cursor-proxy/config.yaml`. That fallback is `deepseek-flash`. Set `model: deepseek-v4-pro` and restart the launcher when the fallback should be Pro.

For normal use, pick two entries in Cursor: **GPT-5.6 Sol** is Pro, **GPT-5.6 Terra** is Flash.

## Sub-agents

Cursor can run several sub-agents at once. Each one gets its own conversation and its own DeepSeek thinking history, the same way native Claude, GPT, and Grok chats do. A sub-agent does not read or overwrite another agent's reasoning.

The proxy keeps that thinking in a local cache because Cursor does not send `reasoning_content` back. A chat that is still running is not deleted by cleanup. Old, finished chats are.

## If something fails

- **`reasoning_content` must be passed back** — the request missed the proxy. The Base URL must be the ngrok URL ending in `/v1`, and the launcher window must still be open.
- **Cursor cannot reach localhost** — use the ngrok URL from the launcher, not `127.0.0.1`.
- **Context looks smaller than 1M** — select **GPT-5.6 Sol** or **GPT-5.6 Terra** once so Cursor applies its 1M catalog budget.
- **Provider error after several sub-agents** — the proxy retries a dropped connection to DeepSeek. Restart the launcher if you are on an older build, then send the message again.

Options and flags are listed in [`config.example.yaml`](config.example.yaml).

## License

MIT. Original design and the reasoning-repair core: [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy).
