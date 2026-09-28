<h1 align="center"><img src="assets/logo.png" width="150" alt="deepseek-cursor-proxy logo"><br>DeepSeek Cursor Proxy</h1>

<p align="center"><a href="README.md">English</a> | <b>Русский</b> | <a href="README.zh-CN.md">简体中文</a></p>

DeepSeek в Cursor с окном **1 000 000 токенов**. Параллельные **субагенты держат свой чат и свой thinking**: они больше не делят одну память размышлений.

Это форк [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy) (MIT). Оригинал чинит ошибку `reasoning_content` на вызовах инструментов. Форк добавляет окно 1M, изоляцию субагентов Cursor и запуск в один клик.

## Установка

Нужны [uv](https://docs.astral.sh/uv/), [ngrok](https://ngrok.com/) (один раз `ngrok config add-authtoken`) и [ключ DeepSeek](https://platform.deepseek.com/api_keys).

**Windows.** Склонируйте репозиторий и дважды щёлкните `Start DeepSeek Proxy.cmd`. Окно не закрывайте. Лаунчер напечатает Base URL и скопирует его в буфер.

**macOS / Linux.**

```bash
git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
./start-deepseek-proxy.sh
```

При первом запуске появится `~/.deepseek-cursor-proxy/config.yaml`. Укажите модель, на которую прокси переписывает запросы Cursor:

```yaml
model: deepseek-flash
```

## Подключение Cursor

1. **Settings → Models → API Keys**
2. Включите **Override OpenAI Base URL** и вставьте адрес из лаунчера. В конце обязательно `/v1`.
3. В **OpenAI API Key** вставьте ключ DeepSeek (`sk-...`).
4. В выборе модели укажите **GPT-5.6 Sol**. Cursor выделит бюджет 1M, а отвечать будет DeepSeek.
5. Свой API включается и выключается сочетанием `Ctrl+Shift+0` (Windows/Linux) или `Cmd+Shift+0` (macOS).

<img src="assets/cursor_config.png" width="600" alt="Настройки ключа и Base URL в Cursor">

После первого чата можно выбрать модель `deepseek-flash` или `deepseek-v4-pro`. Имена на `deepseek-` уходят в DeepSeek как есть. Любое другое имя, включая GPT-5.6 Sol, заменяется на `model` из `config.yaml`.

## Субагенты

Cursor может запустить несколько субагентов сразу. У каждого свой разговор и своя история thinking DeepSeek, как у встроенных чатов Claude, GPT и Grok. Один субагент не читает и не затирает размышления другого.

Прокси хранит thinking локально: Cursor не возвращает поле `reasoning_content`. Очистка не удаляет чат, который ещё идёт. Удаляются только старые, уже законченные разговоры.

## Если что-то не работает

- **`reasoning_content` must be passed back** — запрос прошёл мимо прокси. Base URL должен быть адресом ngrok и заканчиваться на `/v1`, окно лаунчера должно быть открыто.
- **Cursor не принимает localhost** — берите адрес ngrok из лаунчера, не `127.0.0.1`.
- **Контекст меньше 1M** — один раз выберите **GPT-5.6 Sol**, чтобы Cursor выдал бюджет каталога 1M.
- **Ошибка провайдера после нескольких субагентов** — прокси сам повторяет оборвавшееся соединение с DeepSeek. Если сборка старая, перезапустите лаунчер и отправьте сообщение ещё раз.

Остальные параметры — в [`config.example.yaml`](config.example.yaml).

## Лицензия

MIT. Исходная схема и восстановление reasoning: [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy).
