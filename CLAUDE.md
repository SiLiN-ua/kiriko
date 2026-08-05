# Kiriko (霧子) — OSINT Platform

Flask-based платформа для проверки кандидатов и сотрудников оборонной компании на red flags. Работает в паре с Claude AI и Telegram-ботами.

## Назначение

Оператор загружает анкету кандидата → пишет "Проверка" → Kiriko автоматически:
1. Извлекает ІПН/телефон/ФИО из анкеты
2. Запускает пайплайн через Telegram-боты (Nemezida, Sherlock, FanStatistik) + ThunderTrace
3. Формирует структурированный OSINT-отчёт

## Архитектура

```
Browser → nginx → Flask (app.py) → Claude Agent (agent.py) → Tool Router (tools.py)
                                                              ├── Telegram MTProto (Telethon)
                                                              │    ├── Nemezida bot
                                                              │    ├── Sherlock bot
                                                              │    └── FanStatistik bot
                                                              └── ThunderTrace (350+ платформ)
```

**Технологии:**
- Flask, threaded=True (несколько потоков одновременно)
- Anthropic API — модель `claude-haiku-4-5`, max_tokens 8096, streaming
- Telethon MTProto — выделенный asyncio event loop в отдельном потоке
- SSE (Server-Sent Events) для стриминга ответов в браузер
- Парсинг документов: pymupdf (PDF), python-docx (.docx), antiword (.doc)

## Ключевые файлы

- `app.py` — Flask endpoints, парсинг файлов, история, rate limits, watchdog
- `core/agent.py` — Claude API loop, tool-use handling, обрезка контекста
- `core/tools.py` — Telegram-боты, ThunderTrace, определения инструментов
- `templates/splash.html` — экран входа (пароль)
- `templates/chat.html` — основной интерфейс
- `.env` — API ключи, пароль, имена ботов (НЕ коммитить)
- `kiriko_launch.bat` + ярлык на рабочем столе — быстрый запуск

## Порты и хосты

- **Локально (домашний ноут):** `http://127.0.0.1:5099`
- **Продакшн:** `https://kiriko.tetrao.website` (SOLLUTIUM, Амстердам, IP 194.58.47.196)
- **SSH сервера:** `ssh -p 2261 karasu@194.58.47.196`
- **systemd:** `kiriko.service` в `/opt/kiriko/`
- **nginx IP whitelist:** только 188.163.182.194 (рабочий IP)

## Rate limits и защита

- **Per-session:** 5 запросов/мин, 20/час — при превышении 429
- **Anthropic retry:** до 3 попыток с backoff при RateLimitError
- **Context trimming:** tool_result обрезается до 40К символов, хранятся только последние 20 пар сообщений
- **Idle shutdown:** если 60с нет heartbeat от браузера — Flask сам убивается (память освобождается)

## Правила работы Claude

### БЕЗОПАСНОСТЬ (никогда не нарушать)

1. **НЕ трогать Telegram-сессию** без явной команды — потеря сессии = долгий процесс восстановления через QR-код
2. **НЕ делать тестовых запросов** к Anthropic API — денег в аккаунте мало
3. **Имена ботов НЕ показывать публично** — только через env-переменные (`TG_BOT_NEMEZIDA`, `TG_BOT_SHERLOCK`, `TG_BOT_FANSTATISTIK`)
4. **`.env` в .gitignore** — никогда не коммитить

### ЛОГИКА ПРОВЕРКИ

- **Родственники:** тянуть только тех, у кого явное семейное слово (мати/батько/брат…) ИЛИ совпадение прізвища. По адресу — ІГНОРУВАТИ.
- **Nemezida:** один запрос по ІПН кандидата. По ІПН родичів — не запускати. Виняток: якщо родич не знайдений — один додатковий запит "ПІБ ДД.ММ.РРРР".
- **Триггер:** сообщение "Проверка" запускает полный пайплайн без уточнений.

### СТИЛЬ

- «в Украине» / «in Ukraine» — никогда не «на Украине»
- Досье — украинский язык, структура строго по эталону

## Деплой на сервер

Файлы правятся локально → пушатся через SCP → рестарт systemd. Прямой SSH только с рабочего IP.

```bash
# С рабочего ноута:
scp -P 2261 -i "C:\Users\yehor.selin\.ssh\id_ed25519" "путь\файл" karasu@194.58.47.196:/tmp/файл
ssh -p 2261 -i "C:\Users\yehor.selin\.ssh\id_ed25519" karasu@194.58.47.196
sudo cp /tmp/файл /opt/kiriko/файл
sudo systemctl restart kiriko
sudo systemctl status kiriko
```

## Git

- **GitHub (публично):** https://github.com/SiLiN-ua/kiriko
- **GitLab (корпоративный):** self-hosted, доступ только с работы
- **Автор коммитов:** `SiLiN-ua <285033530+SiLiN-ua@users.noreply.github.com>` (privacy protection на GitHub блокирует реальный email)

## Восстановление Telegram-сессии

При `AuthKeyDuplicatedError` (сессию использовали с двух IP):
1. `sudo systemctl stop kiriko`
2. `sudo -u kiriko rm /opt/kiriko/tg_session.session`
3. Пересоздать через `make_session.py` (QR-код, авторизация с телефона)
4. `sudo chown kiriko:kiriko /opt/kiriko/tg_session.session`
5. `sudo systemctl start kiriko`

## Известные подводные камни

- **`.doc` файлы** — парсить только через antiword. olefile тянет мусор и раздувает токены (7 страниц → 217К токенов, превышает лимит 200К)
- **PDF** — обрезка до 12000 символов автоматически
- **Windows CMD и Cyrillic** — в `.bat`/`.ps1` только ASCII, иначе кракозябры. Пути с кириллицей через `%~dp0`
- **Порт 5099 (не 5001)** — 5001 занят другим проектом MooN Attacks
- **413 при загрузке файла** — nginx `client_max_body_size 50m` в конфиге
- **Anthropic API TCP timeout** — SOLLUTIUM иногда режет доступ к api.anthropic.com. Обычно проходит само

## Что делать при обновлениях

1. Правки локально в `E:\Работа проекты\Kiriko\`
2. Проверить локально (кликнуть ярлык Kiriko на рабочем столе)
3. `git add ... && git commit && git push` (GitHub)
4. Задеплоить на сервер через SCP (см. выше)
5. GitLab обновить вручную: `git pull github master && git push origin master` с рабочего ноута
