# ТЗ: AI IT Company v2 — платформа автономных агентов

Переход от одного Telegram-бота с захардкоженными ролями (`prompts.py`) к
мульти-агентной платформе: у каждого агента свой бот, права, папка с кодом, и
есть веб-админка. Делается **итерациями** (см. §7), каждая — рабочая.

## 0. Сквозные принципы
- **Единый источник правды — БД** (а не `prompts.py`): агенты, права, задачи, сообщения, навыки.
- **Всё через события**: действия агентов и переходы задач пишутся в `task_events` (для UI и аудита).
- **Безопасность по умолчанию**: опасные действия — по правам + с логом; опасное на хосте — с подтверждением (`approvals`).
- **Лимиты квоты**: каждое автономное действие проходит через бюджет (`quota.py`).

## 1. Целевая архитектура
```
Telegram (N ботов) ── webhook /webhook/{agent_id} ─► FastAPI (один процесс)
                                                       ├─ TelegramManager
                                                       ├─ Orchestrator
                                                       ├─ MessageBus (async pub/sub)
                                                       ├─ AgentRuntime ×N
                                                       ├─ Admin REST + WebSocket
                                                       └─ SQLite/Postgres
Браузер ── админка (React) ── REST/WS ─────────────────┘
Файлы: agents/<slug>/ (код агента) + workspace/ (проекты)
```
- **AgentRuntime** — обёртка агента: LLM, промпт, права, инструменты, папка, бот, инбокс.
- **MessageBus** — внутренний async pub/sub: делегирование, согласие, помощь, статус, чат.
- **Orchestrator** — ведёт задачи (координатор переговоров, не жёсткий супервизор).
- **TelegramManager** — поднимает/гасит ботов, маршрутизирует апдейты по `agent_id`.

## 2. Стек
- Backend: Python 3.12, FastAPI (webhooks + admin API + WS), python-telegram-bot (webhook), LangGraph, SQLModel + Alembic.
- БД: SQLite → Postgres (через SQLModel).
- Frontend: React + Vite + TS, React Flow (граф), WebSocket.
- Шина: in-process asyncio → Redis pub/sub при росте.

## 3. Модель данных
```
agents(id, name, slug, role, system_prompt, model, telegram_token(enc),
       telegram_username, folder_path, enabled, created_at, updated_at)
agent_permissions(agent_id, key, value)      # can_run_shell, can_edit_others, delegate_to=[...], path_scopes=[...]
agent_obligations(agent_id, key, description)
skills(id, name, owner_agent_id, version, manifest_json, path, is_public)
agent_skills(agent_id, skill_id, adopted_from, enabled)
tasks(id, title, description, status, owner_agent_id, parent_task_id, created_by, created_at, updated_at)
task_events(id, task_id, ts, actor_agent_id, type, payload_json)
messages(id, ts, from_agent_id, to_agent_id|null, chat_id|null, kind, text, meta_json)
delegations(id, task_id, from_agent_id, to_agent_id, kind(task|permission), status, reason, ts)
help_requests(id, task_id, requester_id, helper_id, status, summary, ts)
audit_log(id, ts, actor, action, target, details_json)
```

## 4. Фичи

### 1. Свой Telegram-аккаунт у каждого агента
- N ботов (токен на агента), один процесс. **Webhook**: FastAPI `POST /webhook/{agent_id}`; `TelegramManager` хранит `dict[agent_id → Application]` (без `run_polling`, только `process_update`).
- При старте/изменении агента — `setWebhook`; при удалении — `deleteWebhook` + гасим Application.
- Токены — шифрованные (Fernet). Маршрут: апдейт → `AgentRuntime[X].handle_update()` → агент отвечает своим голосом.
- Общий «командный чат» (группа): туда оркестратор и агенты пишут ход работы.

### 2. Права/обязанности + делегирование с согласия
- Права (`agent_permissions`): `can_run_shell`, `can_edit_others_code`, `can_modify_agents`, `delegate_to=[...]`, `path_scopes`, `max_daily_llm_calls`. Проверка на каждом инструменте (`@requires(...)`).
- Делегирование (через MessageBus): A шлёт `DelegationRequest(task, to=B, kind=task|permission)` → проверка прав → B (его LLM) решает `accept|decline` → переназначение владельца/гранта + запись в `delegations`/`task_events`. При decline — другой агент/эскалация. Грант права — с TTL и отзывом.

### 3. Агенты помогают друг другу
- Триггеры: явный («не получается X») или авто (инструмент упал N раз / тест красный / агент сам застрял).
- `HelpRequest(task, summary, scope)` → оркестратор выбирает помощника по компетенции. Помощник получает скоуп-доступ к папке/файлам просящего (файловые инструменты + `can_edit_others_code` + `path_scopes`), правит код/добавляет методы, прогоняет тесты. Запись в `help_requests`/`task_events`.

### 4. Админ-панель: добавлять/менять агентов
- REST: `GET/POST/PATCH/DELETE /api/agents` (имя, роль, промпт, модель, токен, права, обязанности, папка, enabled); валидация токена через `getMe`.
- Hot-reload: `TelegramManager.reload(agent_id)` + `Registry.refresh()` — без рестарта процесса.
- Frontend: страница «Агенты» (таблица + форма, Markdown-редактор промпта, чекбоксы прав). Авторизация (JWT). Стартовый набор ролей сидируется миграцией из `prompts.py`.

### 5. Папка с кодом у агента + стандарт + перенятие фич
- Стандарт `agents/<slug>/`: `manifest.yaml`, `prompt.md`, `skills/<name>/{skill.yaml, impl.py}`.
- Контракт навыка: `BaseSkill.run(ctx, **params) -> SkillResult`; `SkillLoader` сканирует и регистрирует в `skills`.
- Adoption: крутой навык публикуется (`is_public`); другой агент (или ты) перенимает → `agent_skills(adopted_from=A)`. Агент сам может оценить реестр и предложить adopt (с твоим аппрувом). Semver навыков.

### 6. Админка: доска задач + взаимодействие агентов
- Kanban по `tasks.status`; карточка = задача + исполнитель + подзадачи.
- По задаче — лента `task_events` (создано → делегировано A→B → помощь C→B → выполнено) и граф (React Flow): узлы-агенты, рёбра-передачи.
- Live: WebSocket `/ws/events`. API: `GET /api/tasks`, `/api/tasks/{id}/events`, `/api/messages`.

### 7. Личное общение с каждым ботом
- Покрывается Фичей 1. В личке `AgentRuntime[X]` отвечает в своём контексте (память по `chat_id`). Можно поставить задачу прямо в личке. Команды: `/status`, `/tasks`, `/skills`.

### 8. Боты пишут в чат по инициативе
- Событийные триггеры (task finished, error, help needed, skill adopted) → `should_speak()? + текст` → постит в командный чат.
- Периодический «тик» (редко/по простою) — через бюджет.
- Guardrails: rate-limit проактива, дедуп, `proactive_enabled` на агента, глобальный mute, учёт квоты.

## 5. Межагентный протокол (Фичи 2,3,8)
Типизированные Pydantic-сообщения в `MessageBus`, у каждого агента `asyncio.Queue` инбокс:
```
DELEGATE(task,from,to,kind,reason) → ACCEPT/DECLINE(ref,reason)
HELP_REQUEST(task,from,summary,scope) → HELP_RESULT(task,helper,summary)
STATUS(task,from,state,note)
CHAT(from,chat_id,text)
```
Каждое сообщение → запись в `messages`/`task_events`. Решения агента — короткий LLM-вызов со структурированным выходом (как `CeoDecision`).

## 6. Безопасность и лимиты
- Шифрование токенов; админка под авторизацией.
- Права на каждом инструменте; правка чужого кода — только с `can_edit_others_code` + в `path_scopes`.
- Shell — с подтверждением кнопкой (`approvals`).
- Бюджет LLM на агента/день + глобальный (`quota.py`). Полный аудит (`audit_log`).

## 7. Этапы внедрения (каждый — рабочий)
1. **Данные**: таблицы + миграции; перенос ролей из `prompts.py` в БД (`Registry`).
2. **Админка-CRUD агентов** (Фича 4) + сидинг.
3. **Мульти-бот через webhook + личка** (Фичи 1, 7); рефактор на `AgentRuntime`.
4. **MessageBus + делегирование-с-согласием + помощь** (Фичи 2, 3).
5. **Папки агентов + стандарт навыков + adoption** (Фича 5).
6. **Доска задач + граф взаимодействий + live** (Фича 6).
7. **Проактив** (Фича 8) с guardrails.

## 8. Критерии готовности
- Создал агента в админке → у него рабочий бот, ему можно написать в личку.
- A делегирует B; B соглашается/отказывается; передача видна на доске.
- Агент просит помощь; помощник правит его файлы; событие видно в таймлайне.
- Навык перенимается другим агентом из реестра.
- В админке видно задачи и передачи в реальном времени.
- Агент по событию сам пишет в чат, не превышая лимиты.

## Открытые развилки
- **Webhook vs polling** для N ботов: заложен webhook (нужен публичный HTTPS / туннель в dev). Альтернатива — N polling-циклов (проще локально, хуже масштаб).
- **Автономность**: гибрид (оркестратор + согласие агентов) vs полностью децентрализованные переговоры.
