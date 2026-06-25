# 2026-06-19 — Android-приложение «AI Office»: тех-дизайн и оценка

Цель: нативное Android-приложение (Kotlin/Compose, MVI + Clean Arch) — чат с
командой ИИ-агентов, статус команды, доска, активность. Бэкенд — существующий
**AI_Agents** (FastAPI), развёрнутый на сервере в Docker.

## 0. Ключевой тезис
Бэкенд уже есть. Приложение — это **тонкий клиент** к AI_Agents (REST + WebSocket).
Реально нового кода на сервере немного: **chat-API со стримом** + **авторизация**.
Всё остальное (агенты, задачи, бюджеты, активность, система) уже отдаётся по API.

```
Android (Compose/MVI/CleanArch) ──REST+WS+token──▶ AI_Agents (FastAPI) ──▶ агенты ──▶ Docker(VPS)
```

## 1. Что на бэкенде УЖЕ есть
- `GET /api/agents`, `/api/home`, `/api/system`, `/api/tasks(+/{id})`, `/api/activity`,
  `/api/costs`, `/api/budgets`, `/api/routines`, `/api/approvals`, доска (board tools).
- `WS /ws/events` — live-события (задачи, аудит).
- Оркестрация `team_graph.arun_team` (полный цикл CEO→спецы→ответ).

## 2. Что добавить на бэкенде (новое)
1. **Chat-API со стримом** — `POST /api/chat {message, thread_id}` → запускает
   `arun_team` и стримит шаги/ответ. Транспорт: **SSE** (проще) или поверх `/ws`.
   Стрим отдаёт: `delegate` (CEO→спец), `result` (ответ спеца), `final`. ~переиспользует
   уже существующий `on_event` колбэк из `arun_team`.
2. **Авторизация** — сейчас панель без auth (localhost). Для сети: статический
   `API_TOKEN` (Bearer) или JWT; для Mini App уже есть Telegram-auth. Для standalone —
   простой токен в заголовке + (опц.) device-binding.
3. **CORS + bind 0.0.0.0** за реверс-прокси (Caddy/nginx) с TLS.
4. (Опц.) **push-уведомления** — FCM, когда задача закрыта/нужен аппрув.

## 3. Контракт API для приложения
| Экран | Эндпоинт | Метод |
|---|---|---|
| Чат | `/api/chat` | POST (SSE-стрим) |
| Чат (история) | `/api/chat/history?thread_id=` | GET *(новый, тонкий)* |
| Команда | `/api/home` | GET |
| Live | `/ws/events` | WS |
| Доска | `/api/tasks`, `/api/tasks/{id}` | GET |
| Активность | `/api/activity` | GET |
| Стоимость | `/api/costs` | GET |

## 4. Android — архитектура (Clean Arch + MVI)
**Модули (Gradle):**
- `:app` — навигация (Navigation-Compose), DI-сборка, тема.
- `:core` — общее (Result, dispatchers, network base).
- `:data` — Retrofit/OkHttp (REST), OkHttp/Ktor WS + SSE, DTO, мапперы, репозитории-impl.
- `:domain` — модели (Agent, Task, ChatMessage, TeamSnapshot), use-cases, интерфейсы репо.
- `:feature:chat`, `:feature:team`, `:feature:board`, `:feature:activity` — UI + MVI-стор.

**MVI на экран:** `Intent → Reducer/Store → State → Compose`. ViewModel держит
`StateFlow<State>` + `Channel<Effect>`. Один источник истины на экран.

**Стек/либы:** Kotlin 2.x, Compose (BOM), Coroutines/Flow, **Hilt** (DI),
**Retrofit + OkHttp + kotlinx.serialization**, **OkHttp-SSE** (стрим чата),
WebSocket (OkHttp), Coil (аватары), DataStore (токен/настройки), Turbine+JUnit (тесты).

**Экраны MVP:**
1. **Чат** — пузыри сообщений; во время работы стримятся шаги «🧭 CEO → developer», «✅ developer готово», финал. Поле ввода → `SendMessage`.
2. **Команда** — карточки агентов working/idle (как веб-Главная), hero «N закрыто сегодня», системный монитор.
3. **Доска** — канбан-колонки (свайп), карточка задачи (От/Кому/Приоритет/Сложность/даты).
4. **Активность** — лента событий (из `/ws/events` + `/api/activity`).

**Про «single-Activity на фрагментах»:** на Compose каноничнее **Navigation-Compose**
(composable-экраны в одной Activity) вместо фрагментов. Фрагменты — если есть legacy.

## 5. Деплой
**Фаза 1 (рекомендую):** один Docker-образ всей системы + Caddy (TLS) на VPS
(Hetzner CX22 ≈ €4–6/мес). `docker compose up -d`, «рестарт = апдейт». Volume на
`data/` (SQLite) + бэкап. CI: GitHub Actions → build → ssh deploy.

**Фаза 2 (опц., «как в ролике»):** агенты → автономные воркеры, контейнер-на-агента
с песочным bash (контейнер = граница безопасности, можно убрать ручной аппрув),
координация через шину. Дороже по сложности и токенам (N× вызовов).

## 6. Оценка (соло-разработчик)
| Блок | Объём |
|---|---|
| Бэкенд: chat-API (SSE) + auth + CORS/прокси | **~1 нед** |
| Android: каркас (модули, DI, сеть, тема, навигация) | ~1 нед |
| Android: Чат (стрим) + Команда | ~1.5–2 нед |
| Android: Доска + Активность | ~1 нед |
| Деплой (1 контейнер, Caddy, CI, бэкап) | ~0.5–1 нед |
| Полировка/тесты/релиз (Play Internal) | ~1 нед |
| **MVP итого (одно-контейнерная схема)** | **~6–7 недель** |
| Фаза 2: per-agent контейнеры + шина + sandbox-bash | **+2–4 нед** |

**Ежемесячно:** VPS €4–15 + API (free-модели $0 но rate-limited; Claude — основная
статья, ограничивается нашими бюджетами). Per-agent free-аккаунты множат free-лимит.

## 7. Риски
- **Стрим/реконнекты** — мобильная сеть рвётся; SSE/WS нужен авто-reconnect + resume по thread_id.
- **Auth/безопасность** — без auth наружу выставлять нельзя; токен + TLS обязательны.
- **Стоимость токенов на масштабе** (autonomous-режим) — ставить per-agent бюджеты.
- **SQLite на одном контейнере** — ок для соло/малой команды; при росте → Postgres.
- **Play-модерация** — приложение, гоняющее «агентов с bash», описать аккуратно.

## 8. Первый шаг
**chat-API (SSE) + токен-auth** на бэкенде — фундамент для любого внешнего клиента
(и Android, и веб-чат на дашборде). Затем каркас Android против него.
