<h1 align="center"><img src="assets/logo.png" width="150" alt="deepseek-cursor-proxy logo"><br>DeepSeek Cursor Proxy</h1>

<p align="center"><b>English</b> | <a href="README.ru.md">Русский</a> | <a href="README.zh-CN.md">简体中文</a></p>

Use **DeepSeek** inside **Cursor** on **Windows**, with a **1,000,000-token** context window.
Parallel **sub-agents keep their own chat and their own thinking**. They no longer
share one reasoning cache.

This is a fork of [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy) (MIT). The original proxy repairs DeepSeek `reasoning_content` on tool calls. This fork adds the 1M model rewrite, Cursor sub-agent isolation, and the Windows launcher.

## Install

Windows. You need [uv](https://docs.astral.sh/uv/), [ngrok](https://ngrok.com/) (one-time `ngrok config add-authtoken`), and a [DeepSeek API key](https://platform.deepseek.com/api_keys).

Clone this repo and double-click `Start DeepSeek Proxy.cmd`. Leave the window open. It prints the Base URL and copies it to the clipboard.

In that window type `help`, `settings`, `status`, `clear`, or `quit`. `settings verbose on` turns full logs on without a restart. `clear` deletes the local thinking cache. Closing the window stops the proxy. Starting it again replaces a leftover process from a closed window.

```bat
git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
```

The first run creates `%USERPROFILE%\.deepseek-cursor-proxy\config.yaml`. `model` is only the fallback for unknown names. Sol and Terra have their own mapping:

```yaml
model: deepseek-flash
```

## Connect Cursor

1. **Settings → Models → API Keys**
2. Turn on **Override OpenAI Base URL** and paste the URL from the launcher. It must end in `/v1`.
3. Put your DeepSeek key (`sk-...`) in **OpenAI API Key**.
4. Pick a model and an **Effort** from the tables below. Sol and Terra get Cursor's 1M budget; the proxy answers with DeepSeek.
5. The launcher patches Cursor so **Claude, Gemini, Composer, and Grok** stay on your Cursor plan. The DeepSeek key is not sent with them, and they do not go through the proxy. **Sol, Terra, a name starting with `deepseek-`, and any other model** Cursor sends to the OpenAI base URL go through the proxy. An unknown name is not rejected: DeepSeek answers as `model` from the config file. Windows may ask for administrator permission once. Quit Cursor completely and open it again.
6. Allow the user-level Effort hook when Cursor asks. The launcher installs it in `~/.cursor/hooks.json`, so it runs in every workspace, not only this repo. Each model keeps its own Effort. The `reasoning_effort` value in the config file is the fallback when that model has no stored Effort.

<img src="assets/cursor_config.png" width="600" alt="Cursor API key and base URL settings">

| Cursor | DeepSeek |
| --- | --- |
| GPT-5.6 Sol | `deepseek-v4-pro` |
| GPT-5.6 Terra | `deepseek-flash` |
| name starting with `deepseek-` | same name |
| any other name | `model` in `config.yaml` |

The last two rows apply only to requests that go through the proxy. Composer, Grok, Claude, and Gemini do not.

A name that starts with `deepseek-` is sent to DeepSeek unchanged: `deepseek-v4-pro` stays `deepseek-v4-pro`, and `deepseek-flash` stays `deepseek-flash`. You do not edit `config.yaml` for that.

Any other name, besides Sol and Terra, is dropped. The proxy uses the `model:` line in the config file. That fallback is `deepseek-flash`. Set `model: deepseek-v4-pro` and restart the launcher when the fallback should be Pro.

For normal use, pick two entries in Cursor: **GPT-5.6 Sol** is Pro, **GPT-5.6 Terra** is Flash.

| Cursor Effort | DeepSeek |
| --- | --- |
| None | thinking off (`{"thinking": {"type": "disabled"}}`) |
| Low | thinking on, `reasoning_effort: low` |
| Medium, High | thinking on, `reasoning_effort: high` |
| Extra High, Max | thinking on, `reasoning_effort: max` |

**None** is the fast path: no reasoning tokens. **High** is the normal agent setting. **Max** is for the hardest tasks. Changing model or Effort starts a new thinking cache. If no old tool turn has saved reasoning, the transcript stays. If only some turns match, the older tail can still be dropped and the next reply can start with `[deepseek-cursor-proxy] Refreshed reasoning_content history.`

## Tools

Cursor agent tools work through the proxy, including ApplyPatch. DeepSeek does not accept Cursor's custom ApplyPatch tool, so the proxy turns it into a function call and sends the patch text back for Cursor to apply. The next request in that chat reuses the saved thinking.

## Sub-agents

Cursor can run several sub-agents at once. Each one gets its own conversation and its own DeepSeek thinking history. A sub-agent does not read or overwrite another agent's reasoning.

The proxy keeps that thinking in a local cache because Cursor does not send `reasoning_content` back. A chat that is still running is not deleted by cleanup. Old, finished chats are.

## If something fails

- **`reasoning_content` must be passed back** — the request missed the proxy. The Base URL must be the ngrok URL ending in `/v1`, and the launcher window must still be open.
- **Cursor cannot reach localhost** — use the ngrok URL from the launcher, not `127.0.0.1`.
- **Context looks smaller than 1M** — select **GPT-5.6 Sol** or **GPT-5.6 Terra** once so Cursor applies its 1M catalog budget.
- **Effort stays on the config value** — allow the user hook in `~/.cursor/hooks.json` when Cursor asks, then send again. The launcher installs that hook.
- **Provider error after several sub-agents** — the proxy retries a dropped connection to DeepSeek. Restart the launcher and send the message again if it still fails.

Options and flags are listed in [`config.example.yaml`](config.example.yaml).

## License

MIT. Original design and the reasoning-repair core: [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy).
