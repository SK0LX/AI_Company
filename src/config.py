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
}

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

    # Admin panel + bot host (single process). Bind to localhost by default —
    # the panel has no auth yet, so do not expose it on a public interface.
    admin_host: str = "127.0.0.1"
    admin_port: int = 8100

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
