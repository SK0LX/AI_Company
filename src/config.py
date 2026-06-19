"""Application settings loaded from environment / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# Default specialist model per provider when AGENT_MODEL is not set explicitly.
# NOTE (google): avoid `gemini-flash-latest` — it tracks the newest flash model,
# whose free-tier cap is only ~20 requests/day, which a multi-agent run
# exhausts almost immediately. The 2.5 line has a much more generous free tier.
# NOTE (openrouter): only free models with tool-calling support are used, since
# specialists (developer/frontend) call file tools and the CEO uses structured
# output. Free models there have daily request caps too — see README/.env.
DEFAULT_MODELS = {
    "google": "gemini-2.5-flash-lite",
    "anthropic": "claude-sonnet-4-6",
    "openrouter": "openai/gpt-oss-120b:free",
    # Any OpenAI-compatible endpoint (Groq, Together, vLLM, local, ...). No
    # sensible default model — must be set per agent (or via AGENT_MODEL).
    "openai_compatible": "",
}

# Providers selectable per agent (and globally via LLM_PROVIDER).
SUPPORTED_PROVIDERS = ("openrouter", "anthropic", "google", "openai_compatible")

# Optional stronger default for the CEO (orchestration + structured routing).
# Falls back to DEFAULT_MODELS[provider] when the provider isn't listed here.
DEFAULT_CEO_MODELS = {
    "openrouter": "nvidia/nemotron-3-super-120b-a12b:free",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "openrouter" (free models, OpenAI-compatible), "google" (Gemini free tier)
    # or "anthropic" (Claude, paid API).
    llm_provider: str = "openrouter"

    google_api_key: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    telegram_bot_token: str

    # Empty -> falls back to DEFAULT_MODELS[llm_provider].
    agent_model: str = ""
    # Empty -> reuses the resolved agent model for the CEO too.
    ceo_model: str = ""

    max_tokens: int = 4096
    temperature: float = 0.3

    # Where the SQLite conversation memory lives (persists across restarts).
    db_path: str = "data/memory.sqlite"

    # Shared workspace where the team writes real project files. Each project
    # gets its own subfolder. The developer/frontend file tools are sandboxed to
    # this directory — they cannot read or write outside it.
    workspace_dir: str = "/Users/daniilbutakov/test"

    # Long-term team knowledge base, stored as plain Markdown so you can open the
    # folder directly as an Obsidian vault. When enabled, the CEO reads relevant
    # notes before starting a request and writes a short project card when a task
    # finishes — so the team "remembers" past projects and decisions across runs.
    enable_wiki: bool = True
    wiki_dir: str = "~/ObsidianAITeam"

    # Let the Developer agent actually RUN Python to verify code. This executes
    # model-generated code on your machine, so it is OFF by default. Only enable
    # it in a trusted setting (not a public group).
    enable_code_execution: bool = False
    code_exec_timeout: int = 15

    # Let agents run real shell commands (npm install, pytest, docker compose,
    # ...) in the project folder. This executes model-chosen commands ON YOUR
    # MACHINE, so it is OFF by default and every command requires your approval
    # via a Telegram button (see command_approval_timeout). shell_timeout caps a
    # single command (installs/builds can be slow).
    enable_shell_execution: bool = False
    shell_timeout: int = 300
    command_approval_timeout: int = 600

    # Self-modification: let the `maintainer` agent edit THIS application's OWN
    # source code (the running bot itself) instead of a sandbox project. It works
    # on a git branch, runs the tests and reports a diff — it never pushes or
    # restarts. OFF by default: it points the file/shell tools at the real repo,
    # so only enable it in a trusted, version-controlled checkout (and you'll
    # want enable_shell_execution=true so it can branch/test). self_repo_dir is
    # the repo root; empty = auto-detect the checkout that contains this package.
    enable_self_modify: bool = False
    self_repo_dir: str = ""
    # When on, the maintainer works inside an isolated `git worktree` on a fresh
    # branch (in a temp dir) instead of editing the live working tree in place —
    # the running bot is never disturbed and the change is trivially reviewable.
    # Falls back to in-place editing if git/worktree is unavailable.
    self_worktree: bool = True
    self_worktree_dir: str = ""  # empty = <tempdir>/aiagents-worktrees

    # Budgets & cost control: every model call is metered into a cost ledger
    # regardless. When enable_budget is on, BudgetPolicy hard-stops are ENFORCED —
    # an over-budget agent/company is paused until the window resets or the limit
    # is raised. OFF by default so nothing blocks unexpectedly.
    enable_budget: bool = False

    # Routines / heartbeats: recurring jobs that wake the team or one agent on a
    # schedule and post the result to the team chat. OFF by default; needs
    # team_chat_id set to post. routines_tick_seconds is how often the scheduler
    # checks for due routines.
    enable_routines: bool = False
    routines_tick_seconds: int = 30

    # Stream the internal agent-to-agent chatter (CEO delegations + each
    # specialist's reply) into the chat, not just the final answer.
    show_team_chatter: bool = True

    # The team works internally in English. When true, the mirrored chatter is
    # translated into Russian for the user (one extra model call per specialist
    # reply). Turn off to save quota — chatter then shows the English originals.
    translate_chatter: bool = True

    # OpenRouter free tier: the shared per-account daily request cap. It's 50/day
    # under $10 balance and 1000/day once you add $10 (free models stay free).
    # We estimate remaining requests by counting our own calls and warn the user
    # when only `free_daily_warn_at` are left. Bump free_daily_limit to 1000 here
    # if you add credits.
    free_daily_limit: int = 50
    free_daily_warn_at: int = 10

    # Delegation-with-consent (v2 stage 4): before a specialist runs, ask it (an
    # extra LLM call) whether it accepts the hand-off; on decline the CEO re-routes.
    # OFF by default because it spends one more model call per delegation, which
    # matters on the OpenRouter free daily cap. Task/event recording and the help
    # flow run regardless of this flag (they cost no LLM).
    enable_negotiation: bool = False

    # Group presence (live multi-human group chat): the agent bots behave like
    # colleagues — usually silent, at most one replies at a time, they can talk to
    # each other for a couple of turns, and can do real work (files) per their
    # permissions. Loops are impossible (only HUMAN messages start a turn-burst,
    # which is hard-capped). Tune to taste; set enable_group_chat=false to mute.
    enable_group_chat: bool = True
    group_max_agent_turns: int = 2  # agent contributions per human message (cap)
    group_cooldown: int = 6  # min seconds between two agent posts in a chat
    group_ambient_prob: float = 0.15  # chance to consider chiming into idle chatter
    group_history_turns: int = 16  # recent group messages kept as context

    # Proactive posting (v2 stage 7): agents post short updates to the team chat
    # on events (task done, help needed, declined). OFF by default. Requires a
    # team chat id and the per-agent `proactive` permission; guardrails (rate
    # limit, dedup, mute) always apply. proactive_min_interval is the global
    # cooldown between any two proactive posts (seconds).
    enable_proactive: bool = False
    team_chat_id: int = 0  # Telegram chat the team posts into (0 = disabled)

    # "Задачник" — a dedicated Telegram chat the task tracker auto-posts the task
    # lifecycle into (created / delegated / in-progress / done / cancelled), e.g.
    # "✅ Задача #102 закрыта · исполнитель: … · от: …". 0 = disabled.
    task_channel_id: int = 0
    proactive_min_interval: int = 30
    proactive_max_per_window: int = 5  # per agent
    proactive_window: int = 300  # seconds for the per-agent rate window

    # Admin panel + bot host (single process). Bind to localhost by default —
    # the panel has no auth yet, so do not expose it on a public interface.
    admin_host: str = "127.0.0.1"
    admin_port: int = 8100

    # Telegram Mini App: public HTTPS URL where the dashboard is reachable (e.g.
    # an ngrok/cloudflared tunnel to this server, or a deploy). When set, the bots
    # show a "Дашборд" menu button that opens it inside Telegram. Empty = off.
    webapp_url: str = ""
    # Comma-separated Telegram user IDs allowed to open the Mini App. Empty = any
    # user with a valid Telegram signature (still rejects forged requests).
    webapp_allowed_user_ids: str = ""

    # Secret used to encrypt per-agent Telegram tokens at rest. Leave empty to
    # auto-generate a stable key in data/secret.key (gitignored). Any string
    # works (it is normalized to a valid key).
    app_secret: str = ""

    @property
    def agent_model_resolved(self) -> str:
        return self.agent_model or DEFAULT_MODELS[self.llm_provider]

    @property
    def ceo_model_resolved(self) -> str:
        if self.ceo_model:
            return self.ceo_model
        return DEFAULT_CEO_MODELS.get(self.llm_provider, self.agent_model_resolved)

    @property
    def active_api_key(self) -> str:
        return {
            "google": self.google_api_key,
            "anthropic": self.anthropic_api_key,
            "openrouter": self.openrouter_api_key,
        }.get(self.llm_provider, "")


settings = Settings()  # type: ignore[call-arg]
