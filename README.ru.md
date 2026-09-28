<h1 align="center"><img src="assets/logo.png" width="150" alt="deepseek-cursor-proxy logo"><br>DeepSeek Cursor Proxy</h1>

<p align="center"><a href="README.md">English</a> | <b>Русский</b> | <a href="README.zh-CN.md">简体中文</a></p>

DeepSeek в Cursor на **Windows**, окно **1 000 000 токенов**. Параллельные **субагенты держат свой чат и свой thinking**: они больше не делят одну память размышлений.

Это форк [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy) (MIT). Оригинал чинит ошибку `reasoning_content` на вызовах инструментов. Форк добавляет окно 1M, изоляцию субагентов Cursor и запуск на Windows.

## Установка

Только Windows. Нужны [uv](https://docs.astral.sh/uv/), [ngrok](https://ngrok.com/) (один раз `ngrok config add-authtoken`) и [ключ DeepSeek](https://platform.deepseek.com/api_keys).

Склонируйте репозиторий и дважды щёлкните `Start DeepSeek Proxy.cmd`. Окно не закрывайте. Лаунчер напечатает Base URL и скопирует его в буфер.

```bat
git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
```

При первом запуске появится `%USERPROFILE%\.deepseek-cursor-proxy\config.yaml`. `model` — запасной вариант для неизвестных имён. Sol и Terra мапятся отдельно:

```yaml
model: deepseek-flash
```

## Подключение Cursor

1. **Settings → Models → API Keys**
2. Включите **Override OpenAI Base URL** и вставьте адрес из лаунчера. В конце обязательно `/v1`.
3. В **OpenAI API Key** вставьте ключ DeepSeek (`sk-...`).
4. Выберите модель и **Effort** из таблиц ниже. Sol и Terra получают бюджет 1M от Cursor, отвечает DeepSeek.
5. Лаунчер ставит правку: Composer и Grok работают при включённом ключе OpenAI. Claude и Gemini этот ключ и так не используют. Остальные модели Cursor с включённым ключом по-прежнему не запускаются. Windows один раз может запросить права администратора. После этого полностью перезапустите Cursor.
6. Разрешите проектный хук, когда Cursor спросит. Это `.cursor/hooks.json` в этом репозитории, он срабатывает только пока эта папка открыта как workspace. Каждая отправка сохраняет Effort выбранной модели. Если хук не запустился, прокси берёт `reasoning_effort` из конфига.

<img src="assets/cursor_config.png" width="600" alt="Настройки ключа и Base URL в Cursor">

| Cursor | DeepSeek |
| --- | --- |
| GPT-5.6 Sol | `deepseek-v4-pro` |
| GPT-5.6 Terra | `deepseek-flash` |
| имя на `deepseek-` | то же имя |
| любое другое имя | `model` из `config.yaml` |

Две последние строки касаются только запросов через прокси. Composer, Grok, Claude и Gemini сюда не попадают.

Имя на `deepseek-` уходит в DeepSeek как есть: `deepseek-v4-pro` остаётся `deepseek-v4-pro`, `deepseek-flash` остаётся `deepseek-flash`. `config.yaml` для этого менять не нужно.

Любое другое имя, кроме Sol и Terra, прокси отбрасывает и берёт модель из строки `model:` в конфиге. Сейчас это `deepseek-flash`. Чтобы запасной моделью был Pro, поставьте `model: deepseek-v4-pro` и перезапустите лаунчер.

Для обычной работы достаточно двух пунктов в списке Cursor: **GPT-5.6 Sol** — Pro, **GPT-5.6 Terra** — Flash.

| Effort в Cursor | DeepSeek |
| --- | --- |
| None | thinking выключен (`{"thinking": {"type": "disabled"}}`) |
| Low | thinking включён, `reasoning_effort: low` |
| Medium, High | thinking включён, `reasoning_effort: high` |
| Extra High, Max | thinking включён, `reasoning_effort: max` |

**None** — быстрый путь без reasoning-токенов. **High** — обычная работа агента. **Max** — самые трудные задачи. Смена модели или Effort в том же чате сбрасывает прошлый thinking. Следующий ответ может начаться с `[deepseek-cursor-proxy] Refreshed reasoning_content history.` Дальше сохраняется уже новый thinking.

## Субагенты

Cursor может запустить несколько субагентов сразу. У каждого свой разговор и своя история thinking DeepSeek. Один субагент не читает и не затирает размышления другого.

Прокси хранит thinking локально: Cursor не возвращает поле `reasoning_content`. Очистка не удаляет чат, который ещё идёт. Удаляются только старые, уже законченные разговоры.

## Если что-то не работает

- **`reasoning_content` must be passed back** — запрос прошёл мимо прокси. Base URL должен быть адресом ngrok и заканчиваться на `/v1`, окно лаунчера должно быть открыто.
- **Cursor не принимает localhost** — берите адрес ngrok из лаунчера, не `127.0.0.1`.
- **Контекст меньше 1M** — один раз выберите **GPT-5.6 Sol** или **GPT-5.6 Terra**, чтобы Cursor выдал бюджет каталога 1M.
- **Effort остаётся значением из конфига** — эта папка не открыта как workspace, или хук не разрешён. Разрешите `.cursor/hooks.json` и отправьте сообщение ещё раз.
- **Ошибка провайдера после нескольких субагентов** — прокси сам повторяет оборвавшееся соединение с DeepSeek. Если ошибка остаётся, перезапустите лаунчер и отправьте сообщение ещё раз.

Остальные параметры — в [`config.example.yaml`](config.example.yaml).

## Лицензия

MIT. Исходная схема и восстановление reasoning: [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy).
