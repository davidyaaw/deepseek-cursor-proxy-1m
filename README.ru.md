<h1 align="center"><img src="assets/logo.png" width="150" alt="deepseek-cursor-proxy logo"><br>DeepSeek Cursor Proxy &mdash; форк с контекстом 1M</h1>

<p align="center"><a href="README.md">English</a> | <b>Русский</b> | <a href="README.zh-CN.md">简体中文</a></p>

Прокси-совместимость, которая подключает **Cursor** (и другие
OpenAI-совместимые агенты для кода) к **думающим моделям DeepSeek**. Он
исправляет ошибку `reasoning_content` при вызове инструментов и позволяет
работать с DeepSeek внутри Cursor с полным **контекстным окном на 1 000 000
токенов** (см. [инструкцию по 1M](#контекстное-окно-1m-в-cursor)).

> **Это форк** проекта
> [yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy)
> (лицензия MIT). Вся заслуга за исходную архитектуру и восстановление
> reasoning принадлежит автору оригинала. Форк добавляет фильтрацию
> OpenAI-only инструментов и лаунчеры в один клик —
> см. [Что добавляет форк](#что-добавляет-форк).

---

## Содержание

- [Контекстное окно 1M в Cursor](#контекстное-окно-1m-в-cursor)
- [Что добавляет форк](#что-добавляет-форк)
- [Что умеет прокси](#что-умеет-прокси)
- [Требования](#требования)
- [Установка](#установка)
- [Шаг 1 &mdash; Настройка ngrok](#шаг-1--настройка-ngrok)
- [Шаг 2 &mdash; Запуск прокси](#шаг-2--запуск-прокси)
- [Шаг 3 &mdash; Подключение Cursor к прокси](#шаг-3--подключение-cursor-к-прокси)
- [Шаг 4 &mdash; Чат с DeepSeek в Cursor](#шаг-4--чат-с-deepseek-в-cursor)
- [Справочник по настройкам](#справочник-по-настройкам)
- [Быстрый старт на Windows](#быстрый-старт-на-windows)
- [Другие агенты](#другие-агенты)
- [Решение проблем](#решение-проблем)
- [Как это работает](#как-это-работает)
- [Разработка](#разработка)
- [Благодарности и лицензия](#благодарности-и-лицензия)

---

## Контекстное окно 1M в Cursor

Cursor **не** берёт размер контекста из ответа эндпоинта — он берёт его из
собственного **каталога моделей**. Моделей DeepSeek с окном 1M в этом каталоге
нет, поэтому окно 1M получается так: Cursor думает, что общается с моделью,
у которой оно есть, а прокси незаметно перенаправляет запрос в DeepSeek.

Это работает, потому что прокси заменяет **любое имя модели, не начинающееся
с `deepseek-`**, на резервную модель из `~/.deepseek-cursor-proxy/config.yaml`
(параметр `model:`, например `deepseek-flash`).

### Рецепт

1. Включите свой эндпоинт в Cursor: **Settings &rarr; Models &rarr; API Keys
   &rarr; Override OpenAI Base URL** =
   `https://<ваш-туннель>.ngrok-free.app/v1`, а в поле OpenAI API Key вставьте
   ключ DeepSeek. Включение/выключение — `Ctrl+Shift+0` (Windows/Linux) или
   `Cmd+Shift+0` (macOS).
2. В **выборе модели Cursor выберите `GPT-5.6 Sol`** — модель из каталога
   Cursor с окном **1M**. Cursor отправит в прокси, например,
   `model: "gpt-5.6-sol"` и выделит бюджет до 1M токенов.
3. Прокси видит не-DeepSeek идентификатор и заменяет его на резервную модель
   (`model:` в `config.yaml`), так что **отвечает на самом деле DeepSeek** —
   с бюджетом 1M, который выдал Cursor.
4. После первого запуска выбирать GPT-5.6 Sol уже не обязательно: ваша
   модель **`deepseek-flash`** в Cursor тоже работает с 1M (Cursor даёт
   BYOK-моделям вне каталога 1M по умолчанию), а прокси передаёт
   `deepseek-flash` в DeepSeek без изменений.

```text
Выбор модели в Cursor:  GPT-5.6 Sol        (каталог Cursor: окно 1M)
        |
        |  model = "gpt-5.6-sol"  ->  Override OpenAI Base URL  ->  прокси
        v
   прокси: не-DeepSeek id  ->  замена на резервную (config.yaml: model)
        |
        v
DeepSeek API  (окно 1M токенов)
```

Убедитесь, что `model:` в `config.yaml` — та модель DeepSeek, на которую
должны заменяться не-DeepSeek запросы:

```yaml
# ~/.deepseek-cursor-proxy/config.yaml
model: deepseek-flash
```

### Примечания

- Любой идентификатор `deepseek-*` передаётся в DeepSeek как есть, поэтому
  при работе с `deepseek-flash` ничего не подменяется.
- Cursor всё равно применяет собственный лимит тарифа и считает автосжатие
  контекста от *предполагаемого* окна. Если реальное окно модели DeepSeek
  меньше, ошибка провайдера может прийти раньше, чем сработает сжатие — см.
  [Решение проблем](#решение-проблем).

---

## Что добавляет форк

По сравнению с оригиналом:

- **Отбрасывание OpenAI-only инструментов.** Для моделей с GPT-именами
  Cursor отправляет инструменты вида `{"type": "custom", ...}`; DeepSeek
  отклоняет весь запрос с ошибкой `tools[i].type: unknown variant custom,
  expected function`. Прокси убирает такие записи (и `tool_choice`, который
  на них указывал) и пишет предупреждение вместо ошибки.
- **Лаунчеры в один клик.** `Start DeepSeek Proxy.cmd` +
  `start-deepseek-proxy.ps1` запускают прокси, ждут туннель ngrok, печатают
  Base URL для Cursor и копируют его в буфер обмена. Есть и кроссплатформенный
  `start-deepseek-proxy.sh`.
- **Пример конфигурации** (`config.example.yaml`) с описанием всех параметров.

---

## Что умеет прокси

- **Подставляет `reasoning_content`** в исходящие запросы с вызовами
  инструментов. Cursor не передаёт это поле, поэтому прокси восстанавливает
  reasoning из кэша обычных и потоковых ответов DeepSeek. См.
  [документацию DeepSeek по thinking mode](https://api-docs.deepseek.com/guides/thinking_mode#tool-calls).
- **Показывает размышления DeepSeek в Cursor** — в сворачиваемых
  Markdown-блоках `<details><summary>Thinking</summary>...</details>`.
- **Поднимает туннель ngrok**, чтобы Cursor мог достучаться до локального
  прокси по публичному HTTPS-адресу.
- **Подменяет не-DeepSeek имена моделей** на резервную — именно это даёт
  окно 1M при выборе модели из каталога Cursor.
- **Применяет другие исправления совместимости**, чтобы модели DeepSeek
  нормально работали в Cursor (см. [Что добавляет форк](#что-добавляет-форк)
  и [Как это работает](#как-это-работает)).

## Зачем это нужно

Без прокси связка Cursor + DeepSeek в thinking mode падает на вызовах
инструментов:

<img src="assets/error_400.png" width="600" alt="Ошибка 400 — reasoning_content must be passed back">

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

## Требования

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** (рекомендуется) или `pip`
- **[ngrok](https://ngrok.com/)** (нужен для Cursor, который блокирует
  `localhost` в качестве API URL; не обязателен для агентов, принимающих
  локальный адрес)
- **API-ключ DeepSeek** (`sk-...`), создаётся на
  [platform.deepseek.com](https://platform.deepseek.com/api_keys)

---

## Установка

### Вариант A &mdash; uv (рекомендуется)

```bash
# Установите uv, если его нет
curl -LsSf https://astral.sh/uv/install.sh | sh

# Клонируйте и запустите (uv создаст .venv/ внутри репозитория)
git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
uv run deepseek-cursor-proxy
```

### Вариант B &mdash; pip / conda

```bash
conda create -n dcp python=3.10 -y
conda activate dcp

git clone https://github.com/davidyaaw/deepseek-cursor-proxy-1m.git
cd deepseek-cursor-proxy-1m
pip install -e .

# Запуск
deepseek-cursor-proxy
```

При первом запуске прокси создаёт:

- `~/.deepseek-cursor-proxy/config.yaml` — файл настроек
- `~/.deepseek-cursor-proxy/reasoning_content.sqlite3` — кэш reasoning

---

## Шаг 1 &mdash; Настройка ngrok

Cursor блокирует непубличные API-адреса вроде `localhost`, поэтому прокси
нужен публичный HTTPS-адрес. [ngrok](https://ngrok.com/) открывает доступ к
локальному прокси без проброса портов на роутере. (Можно также использовать
[Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/setup/).)

Создайте аккаунт ngrok, затем один раз установите и авторизуйте его:

```bash
brew install ngrok          # macOS; для других систем см. документацию ngrok
ngrok config add-authtoken <ВАШ_NGROK_AUTHTOKEN>
```

Если прокси нужен только для приложения, которое принимает `localhost`,
пропустите этот шаг: укажите `ngrok: false` в
`~/.deepseek-cursor-proxy/config.yaml` или запускайте с `--no-ngrok`.

## Шаг 2 &mdash; Запуск прокси

```bash
uv run deepseek-cursor-proxy
```

С включённым ngrok прокси печатает публичный адрес при старте:

```text
localhost:9000
api_base_url: https://<ваш-туннель>.ngrok-free.app/v1
```

Если адрес отличается от указанного в Cursor, обновите Base URL в Cursor.

**Фиксированный адрес ngrok / свой домен:** передайте зарезервированный
адрес агенту ngrok:

```yaml
# ~/.deepseek-cursor-proxy/config.yaml
ngrok: true
ngrok_url: https://your-subdomain.ngrok.dev
```

```bash
deepseek-cursor-proxy --ngrok-url https://your-subdomain.ngrok.dev
```

## Шаг 3 &mdash; Подключение Cursor к прокси

В Cursor: **Settings &rarr; Models &rarr; API Keys**, затем:

- Включите **Override OpenAI Base URL** и введите адрес прокси **с `/v1`**.
  ```text
  https://<ваш-туннель>.ngrok-free.app/v1
  ```
- Вставьте **API-ключ DeepSeek** (`sk-...`) в поле OpenAI API Key. Прокси
  передаёт его дальше и нигде не сохраняет — ни в репозитории, ни в кэше.
- **Для окна 1M:** выберите встроенную модель Cursor **`GPT-5.6 Sol`** —
  прокси заменит её на резервную модель DeepSeek из `config.yaml` (см.
  [инструкцию по 1M](#контекстное-окно-1m-в-cursor)).
- Или добавьте модель DeepSeek напрямую в **Model Names**:

  ```text
  deepseek-flash
  deepseek-v4-pro
  ```

<img src="assets/cursor_config.png" width="600" alt="Настройки Cursor для DeepSeek через прокси">

Включение/выключение своего API:

- macOS: `Cmd+Shift+0`
- Windows/Linux: `Ctrl+Shift+0`

## Шаг 4 &mdash; Чат с DeepSeek в Cursor

Выберите в Cursor **`GPT-5.6 Sol`** (для окна 1M) или свою модель
`deepseek-flash` и работайте в режиме чата или агента как обычно.
Размышления модели появляются в сворачиваемом блоке **Thinking**.

<img src="assets/cursor_chat.png" width="480" alt="Чат с DeepSeek через прокси">

---

## Справочник по настройкам

Постоянные настройки хранятся в `~/.deepseek-cursor-proxy/config.yaml`.
Пример с комментариями — [`config.example.yaml`](config.example.yaml). Любой
параметр можно переопределить флагом командной строки.

| Ключ `config.yaml` | Флаг CLI | По умолчанию | Назначение |
| --- | --- | --- | --- |
| `host` | `--host` | `127.0.0.1` | Адрес привязки |
| `port` | `--port` | `9000` | Порт |
| `base_url` | `--base-url` | `https://api.deepseek.com` | Базовый URL API DeepSeek |
| `model` | `--model` | `deepseek-v4-pro` | Резервная модель **и цель подмены** для не-DeepSeek id (например, GPT-5.6 Sol) |
| `thinking` | `--thinking` | `enabled` | `enabled` / `disabled` |
| `reasoning_effort` | `--reasoning-effort` | `max` | `low` \| `medium` \| `high` \| `max` \| `xhigh` |
| `display_reasoning` | `--display-reasoning` | `true` | Показывать размышления в ответе Cursor |
| `collasible_reasoning` | `--collapsible-reasoning` | `true` | Сворачивать блок размышлений |
| `ngrok` | `--ngrok` / `--no-ngrok` | `true` | Запускать туннель ngrok |
| `ngrok_url` | `--ngrok-url` | &mdash; | Фиксированный/зарезервированный адрес ngrok |
| `verbose` | `--verbose` | `false` | Логировать метаданные и тела запросов |
| `request_timeout` | `--request-timeout` | `300` | Таймаут запроса к DeepSeek (секунды) |
| `max_request_body_bytes` | `--max-request-body-bytes` | `20971520` | Максимальный размер запроса |
| `cors` | `--cors` / `--no-cors` | `false` | Отдавать разрешающие CORS-заголовки |
| `reasoning_content_path` | `--reasoning-content-path` | `reasoning_content.sqlite3` | Путь к кэшу reasoning |
| `missing_reasoning_strategy` | `--missing-reasoning-strategy` | `recover` | `recover` (мягко) или `reject` (строго) |
| `reasoning_cache_max_age_seconds` | `--reasoning-cache-max-age-seconds` | `2592000` | Максимальный возраст записи кэша (30 дней) |
| `reasoning_cache_max_rows` | `--reasoning-cache-max-rows` | `100000` | Лимит записей в кэше |

Другие флаги: `--config <путь>` (другой файл настроек), `--trace-dir <папка>`
(полные структурированные трассировки), `--clear-reasoning-cache` (очистить
кэш и выйти). Историческая опечатка `collasible_reasoning` принимается наравне
с `collapsible_reasoning`.

Полезные примеры:

```bash
# Скрыть размышления в интерфейсе Cursor
deepseek-cursor-proxy --no-display-reasoning

# Подробное логирование
deepseek-cursor-proxy --verbose

# Только localhost (для агентов, принимающих локальные адреса)
deepseek-cursor-proxy --no-ngrok --port 9000

# Сменить модель, на которую подменяются не-DeepSeek id (например, GPT-5.6 Sol)
deepseek-cursor-proxy --model deepseek-flash

# Очистить локальный кэш reasoning
deepseek-cursor-proxy --clear-reasoning-cache
```

---

## Быстрый старт на Windows

1. Установите Python, `uv` и `ngrok` (один раз авторизуйте ngrok, см.
   [Шаг 1](#шаг-1--настройка-ngrok)).
2. Дважды щёлкните **`Start DeepSeek Proxy.cmd`** в корне репозитория.

Лаунчер запускает прокси и туннель ngrok, ждёт публичный адрес, печатает его
и копирует в буфер обмена:

```text
================================================================
 DeepSeek proxy is READY
================================================================

 Cursor Base URL (already copied to clipboard):

 https://<ваш-туннель>.ngrok-free.app/v1

 Paste into: Settings -> Models -> API Keys -> Override OpenAI Base URL
 For 1M context: select GPT-5.6 Sol (or your deepseek-flash)
 Toggle custom API:  Ctrl+Shift+0
```

Держите окно открытым, пока работаете в Cursor; закройте его (или нажмите
`Ctrl+C`), чтобы остановить прокси.

По умолчанию лаунчер работает из своей папки, так что настраивать пути не
нужно. Чтобы указать другую, передайте `-ProxyDir <путь>`:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-deepseek-proxy.ps1 -ProxyDir 'C:\path\to\deepseek-cursor-proxy-1m'
```

На macOS/Linux используйте shell-лаунчер:

```bash
./start-deepseek-proxy.sh
```

---

## Другие агенты

Укажите адрес прокси как base URL в любом OpenAI-совместимом клиенте. Агенты,
принимающие локальный эндпоинт, могут использовать
`http://127.0.0.1:9000/v1` напрямую (`ngrok: false`). Любой идентификатор
модели, не начинающийся с `deepseek-`, подменяется на резервную модель — это
удобно для клиентов, которые разрешают только имена из своего каталога.

Эндпоинты прокси:

| Метод | Путь | Назначение |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat completions (единственный поддерживаемый POST) |
| `GET` | `/v1/models` | Список моделей |
| `GET` | `/v1/healthz` | Проверка работоспособности |

---

## Решение проблем

- **`The reasoning_content in the thinking mode must be passed back`** —
  убедитесь, что запрос действительно идёт через прокси (Base URL
  заканчивается на `/v1`), и дайте обычному ходу завершиться, чтобы reasoning
  попал в кэш. Если в кэше нет записей, в терминале будет
  `context status=missing`.
- **`tools[i].type: unknown variant custom`** — в этом форке исправлено:
  такие инструменты отбрасываются с предупреждением. Если ошибка осталась —
  обновитесь до этого форка.
- **Контекст в Cursor меньше 1M** — выберите **`GPT-5.6 Sol`**, как описано
  в [инструкции по 1M](#контекстное-окно-1m-в-cursor). Также проверьте, что
  `model:` в `config.yaml` — нужная модель DeepSeek.
- **Ошибка провайдера раньше, чем сработало автосжатие** — Cursor считает
  сжатие от предполагаемого окна (1M). Если реальное окно модели DeepSeek
  меньше, переключитесь в режим Auto, чтобы сжатие прошло, затем вернитесь
  обратно.
- **Cursor не видит прокси** — Cursor блокирует `localhost`; используйте
  адрес ngrok. Проверьте, что туннель поднят и суффикс `/v1` на месте.
- **`Certificate verify failed`** — обновите ngrok и заново выполните
  `ngrok config add-authtoken`.

---

## Как это работает

- **Основное исправление.** При
  [вызове инструментов в thinking mode](https://api-docs.deepseek.com/guides/thinking_mode#tool-calls)
  DeepSeek требует возвращать в следующих запросах всю **многоходовую**
  цепочку `reasoning_content`. Cursor это поле не передаёт, отсюда ошибка
  400. Прокси (`Cursor -> ngrok -> прокси -> DeepSeek API`) сохраняет
  исходный `reasoning_content` от DeepSeek и подставляет недостающие блоки
  обратно в историю вызовов инструментов.
- **Подмена не-DeepSeek моделей.** Любой id модели, не начинающийся с
  `deepseek-`, заменяется на резервную модель (`model:` в `config.yaml`).
  Вместе с каталогом Cursor это и даёт окно 1M — см.
  [Контекстное окно 1M в Cursor](#контекстное-окно-1m-в-cursor).
- **Изоляция диалогов.** Ключи кэша привязаны к SHA-256 хэшу канонического
  префикса диалога (роли, содержимое и вызовы инструментов без
  `reasoning_content`), а также к модели, конфигурации и хэшу API-ключа.
  Разные диалоги получают разные области, поэтому повторяющиеся id вызовов
  инструментов не конфликтуют. Байт-в-байт одинаковые истории получают
  одинаковые области.
- **Совместимость с кэшем контекста.** Прокси не добавляет искусственных id
  диалогов, временных меток или служебных сообщений и восстанавливает
  `reasoning_content` в точности как было, так что повторяющиеся префиксы
  остаются нетронутыми для
  [кэша контекста DeepSeek](https://api-docs.deepseek.com/guides/kv_cache).
  Процент попаданий в кэш пишется в лог.
- **Прочие исправления совместимости.** Устаревшие поля
  `functions`/`function_call` преобразуются в `tools`/`tool_choice`;
  семантика обязательного и именованного выбора инструмента сохраняется;
  алиасы `reasoning_effort` нормализуются; OpenAI-only записи
  `tools[].type: "custom"` отбрасываются; блоки с размышлениями вырезаются из
  содержимого ответов ассистента; составные массивы content превращаются в
  обычный текст; `reasoning_content` дублируется в видимые в Cursor
  Markdown-блоки details.

---

## Разработка

Юнит-тесты:

```bash
uv run python -m unittest discover -s tests
```

Pre-commit хуки (форматирование и линтинг):

```bash
uv sync --dev
uv run pre-commit run --all-files
```

Отладка:

```bash
# Подробный вывод
deepseek-cursor-proxy --verbose

# Только localhost, подробно, для тестов через curl
deepseek-cursor-proxy --no-ngrok --port 9000 --verbose

# Полные структурированные трассировки запросов
deepseek-cursor-proxy --verbose --trace-dir ./trace-dumps

# Другой файл настроек
deepseek-cursor-proxy --config ./dev.config.yaml
```

---

## Благодарности и лицензия

Проект — форк
[yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy),
распространяется по [лицензии MIT](LICENSE). Исходный copyright &copy; Yixing
Lao. Спасибо автору оригинала и контрибьюторам за основу прокси и механизм
восстановления reasoning.
