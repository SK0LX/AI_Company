# Деплой AI Office 24/7 (Docker)

Один контейнер крутит всю систему (админка + дашборд/Mini App + Telegram-боты +
агенты). Подходит любой VPS (Hetzner CX22 ≈ €4–6/мес).

## Быстрый старт
```bash
# 1) на сервере: поставить Docker
curl -fsSL https://get.docker.com | sh

# 2) забрать код
git clone <твой-репозиторий> ai-office && cd ai-office

# 3) секреты
cp .env.docker.example .env
nano .env                       # TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY/OPENROUTER_API_KEY, APP_SECRET

# 4) запустить
docker compose up -d --build
docker compose logs -f aiagents # увидеть "Running: team + N agent bot(s)"
```
Дашборд — `http://127.0.0.1:8100` на сервере (локально). Наружу — через Caddy (ниже).

## Обновление («рестарт = апдейт»)
```bash
git pull
docker compose up -d --build     # пересобрать и перезапустить; данные сохраняются
```

## Данные и персистентность
Состояние живёт в именованных volume'ах (не в образе):
- `aiagents_data` → SQLite (`memory.sqlite`, `app.sqlite`), `secret.key`, `quota.json`
- `aiagents_workspace` → файлы проектов команды
- `aiagents_wiki` → Obsidian-вики (память)

Бэкап: `docker run --rm -v aiagents_data:/d -v $PWD:/b alpine tar czf /b/data-backup.tgz -C /d .`

## HTTPS (для Mini App)
1. Указать домен в `Caddyfile` (A-запись → IP сервера).
2. Раскомментировать сервис `caddy` и порты 80/443 в `docker-compose.yml`.
3. `docker compose up -d`. Caddy сам выпустит TLS. Поставить `WEBAPP_URL=https://твой-домен` в `.env`.

## Включение фич (в `.env`)
- `ENABLE_AUTOWORK=true` — агенты сами берут с доски незанятые задачи 24/7 (поставь `ENABLE_BUDGET=true` + бюджет, чтобы не жгло лишнего).
- `ENABLE_ROUTINES=true` — задачи по расписанию.
- `ENABLE_SHELL_EXECUTION=true` — агенты выполняют реальные команды (контейнер = песочница).
- `ENABLE_SELF_MODIFY=true` — maintainer правит собственный код на ветке (+ shell).
- `TASK_CHANNEL_ID=...` — Задачник (лента лайфцикла задач).

## Безопасность
- Дашборд биндится локально (`127.0.0.1:8100`) — наружу только через Caddy/TLS.
- Один поллер на токен: **не запускай второй экземпляр** того же бота (Telegram даст `Conflict`).
- Секреты — только в `.env` (он в `.dockerignore`/`.gitignore`), не в образе.
- `APP_SECRET` — стабильная длинная строка (шифрует per-agent токены в БД).

## Заметки
- Бот опрашивает Telegram (polling) — отдельный реверс-прокси для ботов не нужен; Caddy только для дашборда/Mini App.
- Ресурсы: системный монитор на «Главной» покажет RAM/диск/аптайм контейнера.
