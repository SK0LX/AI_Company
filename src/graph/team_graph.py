"""The multi-agent team graph.

The CEO orchestrates the team via a custom LangGraph supervisor that uses
*structured output* (not tool-handoffs). At each step the CEO returns a typed
decision: either delegate a self-contained task to ONE specialist, or produce
the final answer. This avoids the "parallel tool calls" problem that breaks the
prebuilt tool-handoff supervisor on models like Gemini, and works on any model.

Conversations persist in SQLite (per-chat ``thread_id``), so history survives
restarts. Model calls are wrapped with retry/backoff to ride out rate limits.
The Developer can optionally run Python to verify its code (see config).
"""
from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from typing import Annotated, Any, Awaitable, Callable, Literal, Optional, TypedDict

import aiosqlite
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from src import activity, approvals, budget, collab, quota, selfmod
from src.registry import registry
from src.agents.tools import (
    current_project_files,
    sanitize_project,
    set_current_agent,
    set_project_subdir,
    set_self_edit,
    wiki_index,
    wiki_read_note,
    wiki_search_notes,
    wiki_write_note,
)
from src.config import settings

logger = logging.getLogger(__name__)

# Safety valve: never delegate more than this many times per user request.
# Higher now that a full build can run BA -> SA -> designer -> dev -> frontend ->
# backend_reviewer -> frontend_reviewer -> tester -> reviewer, plus a few
# re-delegations to fix what the reviewers find.
MAX_STEPS = 16

# Exceptions worth retrying (rate limits / transient server errors). Both the
# Google and OpenAI/OpenRouter client libs are optional, so build the tuple from
# whatever is installed.
_RETRYABLE: tuple[type[Exception], ...] = ()
try:
    from google.api_core.exceptions import (
        DeadlineExceeded,
        InternalServerError,
        ResourceExhausted,
        ServiceUnavailable,
    )

    _RETRYABLE += (
        ResourceExhausted,
        ServiceUnavailable,
        InternalServerError,
        DeadlineExceeded,
    )
except Exception:  # noqa: BLE001 - provider lib may differ
    pass
try:
    from openai import (  # used by OpenRouter via langchain-openai
        APIConnectionError,
        APITimeoutError,
        InternalServerError as _OpenAIInternalError,
        RateLimitError,
    )

    _RETRYABLE += (
        RateLimitError,
        APITimeoutError,
        APIConnectionError,
        _OpenAIInternalError,
    )
except Exception:  # noqa: BLE001
    pass
if not _RETRYABLE:
    _RETRYABLE = (Exception,)

ROUTING_INSTRUCTIONS = f"""You operate in an orchestration loop and must return a \
structured decision at every step.

The exact list of specialists you can delegate to is provided separately below \
(it is configured at runtime). Use the exact key shown for each.

Rules:
- PLAN FIRST. For any non-trivial request (anything that will involve building \
something or multiple steps), your FIRST action must be action="propose_plan": \
put a short, clear plan in `plan` (in the user's language) — what you intend to \
build and the main steps / which specialists you'll involve — and end by asking \
the user whether to proceed, change it, or do something else. Do NOT start \
delegating yet; wait for the user's reply. Skip the plan only for small talk or a \
tiny one-step ask. Propose a plan ONLY as your first step — never after work has \
already started, and never again once the user has approved a plan in this \
conversation (after approval, proceed to delegate).
- To assign work: action="delegate", pick ONE `role`, and write a clear, \
self-contained `instruction` for that specialist (include all context they need).
- Delegate to one specialist at a time. You will receive their result and can \
then delegate again or finish.
- If the request is ambiguous or missing critical information you need before \
work can start, use action="clarify" and put your question(s) in `question` \
(in the user's language). Ask only what truly blocks you, in ONE short round; \
don't over-ask. The user's answer arrives as the next message and the \
conversation continues.
- When you have everything you need: action="final" and put the complete, \
well-structured answer for the user in `final_answer`, integrating the \
specialists' contributions.
- For small talk or trivial requests that need no specialist, answer immediately \
with action="final".
- Anything that should be BUILT (code, an app, scripts, configs, a website) must \
be delegated to `developer`/`frontend`, who SAVE it as real files — do NOT write \
substantial code inside `final_answer`. Keep `final_answer` concise: summarize \
what was built and list the saved file paths. A short illustrative snippet is \
fine, but never paste large code dumps (the response is length-limited and long \
JSON gets truncated).
- VERIFY before finishing. Each specialist report ends with a section \
"[Files actually on disk in the project folder now]" — that is the GROUND TRUTH. \
Trust it over any prose claim. Before action="final" on a build task, check that \
every file the work needs actually appears in that list. If a needed file is \
missing or it says "no files were actually saved", delegate AGAIN with an \
explicit instruction to save the specific missing files. Only finish when the \
real file list is complete, and base the file paths in your `final_answer` on \
that real list.

Shared workspace: the team writes real project files under \
`{settings.workspace_dir}`. On your FIRST delegation, choose ONE short, \
descriptive `project` slug for the whole request (e.g. "coffee-landing") and \
reuse that SAME `project` value on every subsequent delegation. The system \
automatically puts each specialist inside that one project folder, so all parts \
live together under it — specialists must use relative paths and must NOT create \
their own top-level project folder. In your `final_answer`, tell the user the \
project folder path where the files were saved.

Project structure (IMPORTANT — this is why files used to end up scattered): \
BEFORE you delegate any build work to `developer` or `frontend`, decide ONE \
explicit project file tree and put it in the `structure` field. Use clear \
top-level folders so parts never mix: `backend/` for all server code (e.g. \
`backend/app/main.py`, `backend/requirements.txt`), `frontend/` for the whole web \
app (e.g. `frontend/src/...`, `frontend/package.json`), `docs/` for docs, and the \
root only for shared files (README.md, docker-compose.yml). Reuse the SAME \
`structure` for every build delegation. The system passes it to each builder and \
they must place files EXACTLY at those paths.

Code review before finishing: after `developer` saves backend code, delegate to \
`backend_reviewer` to read the real files and catch bugs/security/logic errors; \
after `frontend` saves UI code, delegate to `frontend_reviewer` likewise. They \
fix small issues in place and report bigger ones — if they report something that \
needs the original developer, delegate the fix and review again. (This is code \
review; `tester` is separate and defines/writes the actual tests.)

Structure review before finishing: once the code is reviewed, delegate to \
`reviewer` (tech lead). The reviewer compares what is actually on disk against \
your `structure`, relocates misplaced files, removes strays, adds trivial missing \
glue files, and reports anything still missing. If it reports a missing \
component, delegate it to the right specialist, then review again. Only use \
action="final" once the code is reviewed and the structure is correct.

Language: write `reasoning` and every `instruction` in English (the team's \
working language). Write `user_note` and `final_answer` in the USER's language \
(Russian if the user wrote in Russian) — `user_note` is a short, friendly \
one-liner so the user can follow what the team is doing."""

ROUTING_INSTRUCTIONS += (
    "\n\nTask board: the user may ask you to inspect, tidy or CLEAR the team's "
    "TASK BOARD — the kanban they see, with columns new / in_progress / blocked / "
    "review / done / cancelled. This is NOT a build task and needs no project. "
    "Delegate it to ONE specialist (they can view the board, and most can manage "
    "it via board tools) with a clear instruction, e.g. 'clear the whole board "
    "(delete every task)' or 'cancel all done tasks'. The specialist uses "
    "board_overview / board_clear / board_set_status / board_delete to do it."
)

if settings.enable_shell_execution:
    ROUTING_INSTRUCTIONS += (
        "\n\nExecution: the team CAN run real commands (installs, builds and "
        "tests like npm install / npm test / pytest / docker compose) via "
        "`developer`, `frontend` and `tester` — each command is shown to the user "
        "for approval first. So plan REAL verification (e.g. have `tester` "
        "actually run the tests, have `developer`/`frontend` install deps and "
        "build) and base your final answer on the real results, not assumptions. "
        "If the user declined a command, say so honestly."
    )

if settings.enable_self_modify:
    ROUTING_INSTRUCTIONS += (
        "\n\nSelf-modification (changing THIS system's OWN code): some requests "
        "are NOT about building a new project — they ask to change THIS bot / "
        "agent system itself (e.g. 'add Telegram reactions', 'change your CEO "
        "prompt', 'fix a bug in your own code', 'add this feature to yourself', "
        "'improve the current project'). Treat 'the current project', 'this "
        "system', 'yourself' and 'your code' as the EXISTING source repository, "
        "NOT a new build. For these: do NOT create a new workspace project and do "
        "NOT set a `structure`; delegate to `maintainer`, which works directly on "
        "the real repository — it creates a git branch, makes the change, runs "
        "the tests, and reports a diff for a human to review. It never pushes or "
        "restarts the bot. If unsure whether the user means the running system or "
        "a brand-new app, use action='clarify' to ask one quick question first."
    )


class CeoDecision(BaseModel):
    """Structured decision the CEO returns at each orchestration step."""

    reasoning: str = Field(description="One or two sentences: what to do next and why.")
    action: Literal["propose_plan", "delegate", "final", "clarify"] = Field(
        description=(
            "'propose_plan' to show your plan and ask the user to approve it "
            "before any work starts (use this FIRST for non-trivial requests), "
            "'delegate' to assign work to a specialist, 'clarify' to ask the user "
            "a clarifying question before starting, or 'final' to answer now."
        )
    )
    role: Optional[str] = Field(
        default=None,
        description=(
            "Specialist key to delegate to (must be one of the exact keys listed "
            "in the roster). Required when action='delegate'."
        ),
    )
    question: Optional[str] = Field(
        default=None,
        description=(
            "Clarifying question(s) for the user, in their language (Russian if "
            "they wrote in Russian). Required when action='clarify'. Ask only "
            "what truly blocks you, in one short round."
        ),
    )
    plan: Optional[str] = Field(
        default=None,
        description=(
            "Your proposed plan for the user to approve, in their language "
            "(Russian if they wrote in Russian). Required when "
            "action='propose_plan'. Briefly list what you'll build and the main "
            "steps, then ask whether to proceed, change it, or do something else."
        ),
    )
    instruction: Optional[str] = Field(
        default=None,
        description="Self-contained task for the specialist. Required when action='delegate'.",
    )
    project: Optional[str] = Field(
        default=None,
        description=(
            "Short kebab-case folder name for THIS project (e.g. 'task-tracker'). "
            "Use the SAME name for every delegation in the request — all "
            "specialists write into this one project folder."
        ),
    )
    structure: Optional[str] = Field(
        default=None,
        description=(
            "The agreed project file tree (paths relative to the project root), "
            "e.g. 'backend/app/main.py, backend/requirements.txt, frontend/src/..., "
            "frontend/package.json, README.md'. Set this BEFORE delegating build "
            "work and reuse the SAME tree for the whole request, so all builders "
            "place files in the same coherent layout."
        ),
    )
    user_note: Optional[str] = Field(
        default=None,
        description=(
            "One short sentence in the USER's language (Russian if they wrote in "
            "Russian) telling the user what you are doing this step and why. "
            "Required when action='delegate'."
        ),
    )
    final_answer: Optional[str] = Field(
        default=None,
        description="The complete answer for the user. Required when action='final'.",
    )


class TeamState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    findings: list[str]  # specialist outputs gathered during the current request
    next_role: Optional[str]
    instruction: Optional[str]
    reasoning: Optional[str]  # CEO's rationale for the latest decision
    user_note: Optional[str]  # CEO's user-facing one-liner for the latest step
    last_role: Optional[str]  # role that just produced a finding (for display)
    awaiting_input: Optional[bool]  # True when the CEO asked the user a question
    awaiting_kind: Optional[str]  # "plan" (approval) or "clarify" (question)
    plan_approved: Optional[bool]  # user approved the plan -> execute, don't re-plan
    project_dir: Optional[str]  # one folder slug shared by the whole request
    structure: Optional[str]  # agreed project file tree shared by the whole request
    task_id: Optional[int]  # collab Task tracking this request (stage 4)
    steps: int


# --- stage 4: consent + help deciders ---------------------------------------

class ConsentDecision(BaseModel):
    """A specialist's accept/decline answer to a delegation (consent)."""

    accept: bool = Field(description="True to take on the task, False to decline.")
    reason: str = Field(default="", description="One short sentence explaining why.")


async def _consent_decider(to_agent: str, task_text: str, reason: str) -> tuple[bool, str]:
    """Ask agent ``to_agent`` (its own LLM persona) whether it accepts the task.

    Used only when ``settings.enable_negotiation`` is on. Defaults to accept on
    any model error so a hiccup never strands a task."""
    model = _retry(_specialist_model(to_agent).with_structured_output(ConsentDecision))
    system = SystemMessage(
        content=(
            f"{registry.prompt(to_agent)}\n\nA teammate wants to delegate the task "
            "below to you. Decide whether it fits YOUR role and you can take it on. "
            "Accept unless it is clearly outside your role or critical information "
            "is missing. Answer with accept (bool) and a one-sentence reason."
        )
    )
    human = HumanMessage(content=f"Task:\n{task_text}\n\nReason given: {reason or '(none)'}")
    try:
        decision: ConsentDecision = await model.ainvoke([system, human])
        return bool(decision.accept), (decision.reason or "").strip()[:200]
    except Exception:  # noqa: BLE001
        logger.warning("consent decision failed for %s; auto-accepting", to_agent, exc_info=True)
        return True, "auto-accepted (decision failed)"


async def _pick_helper(requester: str, summary: str, candidates: list[str]) -> Optional[str]:
    """Choose a helper for a stuck task. Heuristic (no LLM call, so no quota cost):
    the first enabled candidate that isn't the requester."""
    for slug in candidates:
        if slug != requester and registry.is_specialist(slug):
            return slug
    return None


def _skill_tools_for(role: str) -> list:
    """Build a langchain tool per enabled skill the agent has (owned + adopted),
    so a specialist can call its skills like any other tool. Each tool wraps
    ``run_skill(owner, name, **params)``. Best-effort: never raises."""
    from langchain_core.tools import StructuredTool
    from pydantic import create_model

    from src import skill_registry
    from src.skills import run_skill

    tools: list = []
    try:
        skills = skill_registry.enabled_skills_for(role)
    except Exception:  # noqa: BLE001
        logger.exception("failed to list skills for %s", role)
        return tools

    for skill in skills:
        owner, name = skill["owner"], skill["name"]
        params = skill.get("params") or {}
        try:
            fields = {p: (str, Field(default="", description=str(d))) for p, d in params.items()}
            args_model = create_model(f"skill_{name}_args", **fields) if fields else None

            def _make(owner: str, name: str):
                def _call(**kwargs) -> str:
                    res = run_skill(owner, name, **{k: v for k, v in kwargs.items() if v != ""})
                    return res.output or ("[ok]" if res.ok else "[skill failed]")

                return _call

            origin = "" if skill.get("owned") else f" (adopted from {skill.get('adopted_from')})"
            tools.append(
                StructuredTool.from_function(
                    func=_make(owner, name),
                    name=f"skill_{name}",
                    description=(skill.get("description") or name) + origin,
                    args_schema=args_model,
                )
            )
        except Exception:  # noqa: BLE001 - one bad skill must not block the agent
            logger.exception("failed to build tool for skill %s/%s", owner, name)
    return tools


def _mark_task_done(state: TeamState) -> None:
    """Close out the collab task tracking this request (best-effort)."""
    task_id = state.get("task_id")
    if not task_id:
        return
    try:
        collab.set_task_status(task_id, "done", actor="ceo")
    except Exception:  # noqa: BLE001 - tracking must never break a run
        logger.exception("failed to mark task %s done", task_id)


# --- models -----------------------------------------------------------------

# Anthropic models that REJECT sampling params (temperature/top_p/top_k) — see
# the claude-api skill. Sending temperature to these returns a 400.
_ANTHROPIC_NO_TEMPERATURE = ("claude-opus-4-7", "claude-opus-4-8", "claude-fable-5")


def _global_key(provider: str) -> str:
    return {
        "google": settings.google_api_key,
        "anthropic": settings.anthropic_api_key,
        "openrouter": settings.openrouter_api_key,
        "openai_compatible": settings.openrouter_api_key,
    }.get(provider, "")


def _resolve_spec(slug: Optional[str], *, is_ceo: bool = False) -> tuple[str, str, str, str]:
    """Resolve the (provider, model, api_key, base_url) for an agent.

    Per-agent values from the registry win; otherwise fall back to the global
    settings. This is what lets one agent run on Claude while another runs on an
    OpenRouter free model or any OpenAI-compatible endpoint."""
    provider = ((registry.provider_for(slug) if slug else "") or settings.llm_provider).strip()
    model = (registry.model_for(slug) if slug else "").strip()
    if not model:
        if provider == settings.llm_provider:
            model = settings.ceo_model_resolved if is_ceo else settings.agent_model_resolved
        else:
            from src.config import DEFAULT_CEO_MODELS, DEFAULT_MODELS

            model = (DEFAULT_CEO_MODELS.get(provider) if is_ceo else "") or DEFAULT_MODELS.get(provider, "")
    api_key = (registry.api_key_for(slug) if slug else "") or _global_key(provider)
    base_url = (registry.base_url_for(slug) if slug else "").strip()
    return provider, model, api_key, base_url


def _make_model(provider: str, model_name: str, api_key: str, base_url: str) -> BaseChatModel:
    """Build a LangChain chat model for ANY supported provider."""
    # Meter token usage/cost on every provider (attribution comes from context
    # vars set by the orchestrator). One callback per cached model.
    cost_cb = budget.make_cost_callback(provider, model_name)
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key or settings.google_api_key,
            max_output_tokens=settings.max_tokens,
            temperature=settings.temperature,
            # The SDK retries internally too; keep it low so a hard quota 429
            # fails fast instead of stacking with our own _retry wrapper.
            max_retries=1,
            callbacks=[cost_cb],
        )
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs: dict = dict(
            model=model_name,
            api_key=api_key or settings.anthropic_api_key,
            max_tokens=settings.max_tokens,
            max_retries=1,
            callbacks=[cost_cb],
        )
        # Opus 4.7/4.8 / Fable 5 reject temperature (claude-api skill) — omit it.
        if not any(model_name.startswith(p) for p in _ANTHROPIC_NO_TEMPERATURE):
            kwargs["temperature"] = settings.temperature
        return ChatAnthropic(**kwargs)
    if provider in ("openrouter", "openai_compatible"):
        from langchain_openai import ChatOpenAI

        is_or = provider == "openrouter"
        url = base_url or ("https://openrouter.ai/api/v1" if is_or else "")
        return ChatOpenAI(
            model=model_name,
            api_key=api_key or (settings.openrouter_api_key if is_or else "x"),
            base_url=url or None,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            max_retries=1,
            # Count OpenRouter calls against the free daily cap (warn before it
            # hits), and meter cost on every call.
            callbacks=[quota.counter, cost_cb] if is_or else [cost_cb],
            default_headers=(
                {"HTTP-Referer": "https://github.com/ai-it-company", "X-Title": "AI IT Company"}
                if is_or else None
            ),
        )
    raise ValueError(f"Unknown LLM provider: {provider!r}")


def _retry(runnable: Runnable) -> Runnable:
    """Add exponential backoff so transient rate limits don't fail a turn."""
    return runnable.with_retry(
        retry_if_exception_type=_RETRYABLE,
        stop_after_attempt=3,
        wait_exponential_jitter=True,
    )


@lru_cache(maxsize=32)
def _model_cached(provider: str, model_name: str, api_key: str, base_url: str) -> BaseChatModel:
    return _make_model(provider, model_name, api_key, base_url)


def _ceo_model() -> BaseChatModel:
    return _model_cached(*_resolve_spec("ceo", is_ceo=True))


def _specialist_model(slug: Optional[str] = None) -> BaseChatModel:
    """Model for a specialist. Honors the agent's per-agent provider/model/key/
    base_url from the registry; falls back to the global default."""
    return _model_cached(*_resolve_spec(slug))


# Roles that work with real files and therefore run as tool-enabled agents.
_FILE_TOOL_ROLES = (
    "developer",
    "frontend",
    "tester",
    "backend_reviewer",
    "frontend_reviewer",
    "reviewer",
)

# Code reviewers: read the saved code and fix small bugs in place.
_CODE_REVIEW_ROLES = ("backend_reviewer", "frontend_reviewer")

# Roles allowed to run real shell commands (installs/builds/tests), each gated
# behind the user's per-command approval. The tech-lead reviewer is excluded —
# it only fixes file layout.
_SHELL_ROLES = (
    "developer",
    "frontend",
    "tester",
    "backend_reviewer",
    "frontend_reviewer",
)


def _perm(role: str, key: str) -> bool:
    return registry.permissions(role).get(key) == "true"


def _uses_tools(role: str) -> bool:
    """Whether a specialist runs as a tool-enabled ReAct agent: a built-in tool
    role, or a custom agent granted file/shell/board permissions in the panel."""
    return (
        role in _FILE_TOOL_ROLES
        or _perm(role, "can_edit_files")
        or _perm(role, "can_run_shell")
        or _perm(role, "can_manage_board")
    )


def _is_self_editor(role: str) -> bool:
    """Whether this role edits the app's OWN source (self-modification): the
    built-in ``maintainer`` or any agent granted the ``can_self_modify`` perm.
    Only takes effect when ``settings.enable_self_modify`` is on (see callers)."""
    return role == "maintainer" or _perm(role, "can_self_modify")


def _tool_agent(role: str):
    """Build a ReAct agent for a tool-using specialist. NOT cached, so prompt and
    permission edits made in the admin panel take effect on the next run. Tools
    follow built-in role behavior, plus shell is gated by the `can_run_shell`
    permission."""
    from langgraph.prebuilt import create_react_agent

    from src.agents.tools import (
        board_claim,
        board_clear,
        board_delete,
        board_overview,
        board_release,
        board_set_status,
        budget_remaining,
        delete_file,
        handoff,
        lock_acquire,
        lock_release,
        lock_who,
        list_files,
        list_memory,
        move_file,
        read_file,
        read_memory,
        run_python,
        run_shell,
        save_memory,
        say,
        search_memory,
        write_file,
    )

    base_prompt = registry.prompt(role)
    model = _specialist_model(role)
    # Reusable skills (owned + adopted) become tools alongside the file/shell ones.
    skill_tools = _skill_tools_for(role)
    if skill_tools:
        names = ", ".join(t.name for t in skill_tools)
        base_prompt += (
            f"\n\nYou also have these reusable SKILLS as tools: {names}. Use them "
            "when relevant instead of redoing the work by hand."
        )
    # Shared team memory (the Obsidian knowledge base) — read before, write during.
    memory_tools = (
        [search_memory, read_memory, list_memory, save_memory]
        if settings.enable_wiki
        else []
    )
    if memory_tools:
        base_prompt += (
            "\n\nYou share a TEAM MEMORY (an Obsidian knowledge base). BEFORE you "
            "start, call search_memory / list_memory to reuse what the team already "
            "knows, and read_memory for the details. AS YOU WORK, call save_memory "
            "to record durable knowledge — what you built and HOW it works, key "
            "decisions and WHY, the file map (path — purpose), setup/run/test steps, "
            "and gotchas — so teammates and future runs can read it."
        )
    # Task board: every tool agent can VIEW it + COORDINATE (claim/lock/budget);
    # mutating the board (status/delete/clear) needs can_manage_board.
    board_tools = [board_overview, board_claim, board_release,
                   lock_acquire, lock_release, lock_who, budget_remaining, say, handoff]
    base_prompt += (
        "\n\nCOORDINATION — never double-work with other agents: BEFORE you start on "
        "a shared task or project, CLAIM it (board_claim) or lock the resource "
        "(lock_acquire 'repo:<x>' / 'file:<y>'). If it's busy, take another or wait; "
        "board_release / lock_release when done. Check budget_remaining before "
        "expensive work and self-throttle near the limit. Use say('…') to speak in "
        "the team chat as yourself — address a teammate by name when you need "
        "something from them (e.g. say('@developer, нужен endpoint /react')).\n"
        "STAY IN YOUR LANE — do the part that fits YOUR role, then HAND OFF the rest: "
        "call handoff('<teammate>', '<concrete next step>') to pass the work to the "
        "right colleague (e.g. an analyst clarifies requirements then "
        "handoff('developer', 'реализуй …'); the developer builds then "
        "handoff('tester', 'проверь …')). Don't do another role's job yourself — "
        "pass it on, and the task flows across the team."
    )
    if _perm(role, "can_manage_board"):
        board_tools += [board_set_status, board_delete, board_clear]
        base_prompt += (
            "\n\nYou can VIEW and MANAGE the team's TASK BOARD — the kanban the user "
            "sees, with columns new / in_progress / blocked / review / done / "
            "cancelled. Tools: board_overview (see counts), board_set_status (move a "
            "task), board_delete (remove one), board_clear (tidy/empty the board — "
            "mode='delete' removes tasks permanently, mode='cancel' moves them to the "
            "cancelled column). When the user asks to inspect, tidy or CLEAR the "
            "board, call board_overview first, then act."
        )
    else:
        base_prompt += (
            "\n\nYou can VIEW the team's task board with board_overview (read-only)."
        )
    can_shell = settings.enable_shell_execution and _perm(role, "can_run_shell")
    shell_note = (
        "\n\nYou can run REAL commands with run_shell in the project folder "
        "(e.g. `npm install`, `npm test`, `pytest`, `pip install -r "
        "requirements.txt`, `docker compose up --build`). Each command needs the "
        "user's approval; if they decline you get '[skipped by user]'. Actually "
        "run installs/builds/tests to VERIFY your work instead of assuming it "
        "passes; read the output and fix real failures."
        if can_shell
        else ""
    )

    if role == "tester":
        tools: list = [list_files, read_file, write_file] + skill_tools + memory_tools + board_tools
        if can_shell:
            tools.append(run_shell)
        extra = (
            "\n\nThe project is ALREADY on disk in this folder. Use list_files and "
            "read_file to inspect the code, write_file to add test files, and "
            "run_shell to ACTUALLY run the tests (pytest, npm test) and report the "
            "real results — pass/fail with the relevant output."
            + shell_note
        )
        return create_react_agent(model, tools=tools, prompt=base_prompt + extra)

    if role in _CODE_REVIEW_ROLES:
        tools = [list_files, read_file, write_file] + skill_tools + memory_tools + board_tools
        extra = (
            "\n\nYou are reviewing code that is ALREADY saved in this project's "
            "folder. First call list_files, then read_file on the files in your "
            "area to review the REAL code (not a description). Fix small, "
            "well-contained bugs in place by rewriting the affected file with "
            "write_file (keep changes surgical — do not redesign working code). "
            "For anything large or risky, report it instead of guessing. End with "
            "a concise review: bugs found, what you fixed, what still needs the "
            "developer."
        )
        if role == "backend_reviewer" and settings.enable_code_execution:
            tools.append(run_python)
            extra += (
                " You can call run_python to reproduce a bug or check a fix before "
                "saving it."
            )
        if can_shell:
            tools.append(run_shell)
            extra += shell_note
        return create_react_agent(model, tools=tools, prompt=base_prompt + extra)

    if role == "reviewer":
        tools = ([list_files, read_file, write_file, move_file, delete_file]
                 + skill_tools + memory_tools + board_tools)
        extra = (
            "\n\nYou are reviewing an EXISTING project that is ALREADY on disk in "
            "this project's folder. First call list_files to see everything that "
            "was created and read_file to inspect key files. Compare the real "
            "files against the AGREED PROJECT STRUCTURE you were given, then FIX "
            "layout issues with your tools: use move_file to relocate/rename files "
            "into the correct paths, delete_file to remove stray/empty/duplicate "
            "files, and write_file only for trivial missing glue (package markers, "
            "a short README, an entrypoint). Do NOT rewrite large feature code — "
            "if a whole module/component is missing, report it for the CEO to "
            "delegate. Finish by calling list_files again and reporting the final "
            "structure, what you fixed, and anything still missing."
        )
        return create_react_agent(model, tools=tools, prompt=base_prompt + extra)

    if settings.enable_self_modify and _is_self_editor(role):
        tools = ([list_files, read_file, write_file, move_file, delete_file]
                 + skill_tools + memory_tools + board_tools)
        if can_shell:
            tools.append(run_shell)
        if settings.enable_code_execution:
            tools.append(run_python)
        extra = (
            "\n\nSELF-MODIFICATION MODE. You are editing THIS application's OWN "
            "source repository — the live multi-agent bot itself. You are already "
            "at the repo root and your file tools read/write the REAL project "
            "files (not a sandbox copy). Ignore any 'AGREED PROJECT STRUCTURE' — "
            "work within the existing layout.\n\n"
            "Follow this exact, careful procedure:\n"
            "1. EXPLORE first: list_files and read_file the files relevant to the "
            "request before changing anything. Never edit a file you have not "
            "read.\n"
            "2. BRANCH: you MUST work on a dedicated branch, never the live one. "
            "If the orchestrator already placed you in an isolated worktree on a "
            "fresh branch (the task will say so), USE it — do NOT create another. "
            "Otherwise create one with run_shell: `git checkout -b feat/<name>`.\n"
            "3. EDIT surgically with write_file / move_file — change only what the "
            "task needs, match the surrounding code style, keep the diff minimal.\n"
            "4. TEST: run the suite with run_shell (`python tests/run_all.py`, or "
            "`pytest -q`) and READ the output. Fix real failures you introduced.\n"
            "5. REPORT: run `git --no-pager diff --stat` then `git --no-pager "
            "diff`, and summarize — the branch name, which files changed and why, "
            "the test result (pass/fail with key lines), and the diff. A human "
            "reviews it and decides whether to merge.\n\n"
            "HARD RULES — never break these:\n"
            "- NEVER run `git push`, commit to `main`, merge, deploy, or restart "
            "the bot/process. You stop after producing a reviewable branch + diff.\n"
            "- NEVER write to `.git/`, `.env`, anything under `data/`, or any "
            "secret/key file (the tools block these — don't even try).\n"
            "- If you cannot run shell (declined or disabled), still make the file "
            "edits, then clearly state that branching/tests could not run.\n"
            "- If the change is large, risky or ambiguous, make the smallest safe "
            "step and report what else is needed rather than guessing."
        )
        return create_react_agent(model, tools=tools, prompt=base_prompt + extra)

    # Default: builders (developer/frontend) and custom agents with file perms.
    tools = [write_file, read_file, list_files] + skill_tools + memory_tools + board_tools
    extra = (
        "\n\nYou have a real filesystem workspace and you are ALREADY inside this "
        "project's folder. Use write_file to SAVE every file you produce, with "
        "paths RELATIVE to the project root and grouped in subfolders "
        "(e.g. 'backend/main.py', 'frontend/index.html', 'docs/architecture.md'). "
        "Do NOT prepend the project name to your paths and do NOT create another "
        "top-level project folder.\n\n"
        "CRITICAL — files only count if you actually save them:\n"
        "- Call write_file SEPARATELY for EVERY file. One write_file call = one "
        "file. Do NOT describe a file's contents in prose instead of saving it.\n"
        "- Never claim a file exists unless you saved it with write_file in THIS "
        "turn.\n"
        "- When you are done, call list_files to CONFIRM what is on disk, then "
        "report the exact list of files you saved. If something you intended is "
        "missing from list_files, save it before finishing.\n"
        "Use read_file/list_files to inspect what teammates already created."
    )
    if role == "developer" and settings.enable_code_execution:
        tools.append(run_python)
        extra += (
            " You can also call run_python to execute and verify code before "
            "saving it. Prefer to test non-trivial code at least once."
        )
    if can_shell:
        tools.append(run_shell)
        extra += shell_note
    return create_react_agent(model, tools=tools, prompt=base_prompt + extra)


# --- helpers ----------------------------------------------------------------

def _content_to_text(content: Any) -> str:
    """LLM responses can be a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return str(content)


def _last_user_text(messages: list[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return _content_to_text(msg.content)
    return ""


def _findings_block(findings: list[str]) -> str:
    return "\n\n".join(findings)


async def _synthesize(messages: list[BaseMessage], findings: list[str]) -> str:
    """Force the CEO to write a final answer from whatever has been gathered."""
    prompt = HumanMessage(
        content=(
            "Write the final answer for the user now, integrating the specialists' "
            "results. Reply in the user's language (Russian if the request is in "
            f"Russian).\n\nUser request:\n{_last_user_text(messages)}\n\n"
            f"Specialist results (in English):\n{_findings_block(findings)}"
        )
    )
    resp = await _retry(_ceo_model()).ainvoke(
        [SystemMessage(content=registry.ceo_prompt()), prompt]
    )
    return _content_to_text(resp.content) or "..."


async def atranslate_ru(text: str) -> str:
    """Translate English team output into Russian for display to the user.

    Keeps code, identifiers, paths and technical terms intact. Falls back to the
    original text on any error so the chat never blocks on translation."""
    text = (text or "").strip()
    if not text:
        return text
    prompt = HumanMessage(
        content=(
            "Translate the following text into Russian for the end user. Keep "
            "code blocks, identifiers, file paths, URLs and technical terms "
            "unchanged. Preserve Markdown formatting. Output ONLY the "
            f"translation, with no preamble.\n\n{text}"
        )
    )
    try:
        resp = await _retry(_specialist_model()).ainvoke([prompt])
        return _content_to_text(resp.content) or text
    except Exception:  # noqa: BLE001 - display translation must never hard-fail
        return text


def _has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


async def aensure_russian(text: str) -> str:
    """Guarantee user-facing text is in Russian. Our CEO model sometimes ignores
    the "user's language" instruction (it's a reasoning model that defaults to
    English), so if the text has no Cyrillic at all we translate it. Text that's
    already Russian is returned untouched (no extra model call)."""
    text = (text or "").strip()
    if not text or _has_cyrillic(text):
        return text
    return await atranslate_ru(text)


# --- wiki (long-term team memory, Obsidian-style Markdown vault) ------------

def _wiki_context(query: str) -> str:
    """Build a compact digest of relevant past notes to prime the CEO.

    Pure file search (no model call) so it costs no quota. Returns "" when the
    wiki is disabled/empty so the caller can skip injecting anything."""
    if not settings.enable_wiki:
        return ""
    try:
        hits = wiki_search_notes(query, max_results=5)
        index = wiki_index(max_notes=20)
    except Exception:  # noqa: BLE001 - memory priming must never break a run
        return ""
    no_hits = hits.startswith("[")  # "[no matching wiki notes]"
    empty = index.startswith("[")  # "[wiki is empty]"
    if no_hits and empty:
        return ""
    parts = ["[Team wiki — long-term memory of past projects and decisions]"]
    if not no_hits:
        parts.append(f"Relevant notes for this request:\n{hits}")
    if not empty:
        parts.append(f"All notes (index):\n{index}")
    parts.append(
        "Use this context if it helps. To read a full note, delegate a "
        "specialist or rely on what's summarized here."
    )
    return "\n\n".join(parts)


async def _update_wiki_card(
    project_dir: Optional[str],
    messages: list[BaseMessage],
    findings: list[str],
    answer: str,
) -> None:
    """Write/update the project's KNOWLEDGE NOTE in the shared memory after a task
    finishes — the team's durable record of WHAT was built and HOW it works, so a
    teammate (or a future run) can later read and understand it.

    Best-effort and English (the team's working language). Never raises."""
    if not settings.enable_wiki or not project_dir or not findings:
        return
    try:
        slug = sanitize_project(project_dir)
        existing = wiki_read_note(f"projects/{slug}")
        if existing.startswith("["):  # not found / error markers
            existing = "(none yet)"
        prompt = HumanMessage(
            content=(
                "You are the team's archivist. Update the project's KNOWLEDGE NOTE "
                "in our shared memory (an Obsidian vault) so a teammate who has "
                "never seen this project can later read it and understand WHAT it is "
                "and HOW it works. MERGE new facts into the existing note (keep "
                "still-true details, correct outdated ones, don't lose history). Be "
                "concise but COMPLETE — this is long-term memory, not a status "
                "update. Ground every claim in the specialist results / files that "
                "were actually produced; do not invent. Output ONLY the full "
                "Markdown note (no preamble), with this structure:\n\n"
                f"# {slug}\n\n"
                "## Summary\n(1-3 sentences: what this project is and who it's for)\n\n"
                "## How it works\n(the architecture and main flow — how the parts "
                "fit together end to end, in a few short paragraphs or bullets)\n\n"
                "## Key decisions\n(bullets: decision — why it was made; include "
                "tech/stack/library choices and trade-offs)\n\n"
                "## Structure / files\n(bullets: path — what it does, for the main "
                "files and folders actually on disk)\n\n"
                "## How to run\n(setup / build / run / test commands, if any)\n\n"
                "## Gotchas & TODO\n(pitfalls, known issues, and what's left to do)\n\n"
                "## Status\n(one line: current state)\n\n"
                f"--- Existing note ---\n{existing}\n\n"
                f"--- User request ---\n{_last_user_text(messages)}\n\n"
                f"--- Specialist results ---\n{_findings_block(findings)}\n\n"
                f"--- Final answer delivered to the user ---\n{answer}"
            )
        )
        resp = await _retry(_specialist_model()).ainvoke([prompt])
        card = _content_to_text(resp.content).strip()
        if card:
            wiki_write_note(f"projects/{slug}", card)
    except Exception:  # noqa: BLE001 - wiki write must never break a run
        return


# --- graph nodes ------------------------------------------------------------

async def _ceo_node(state: TeamState, config: Optional[RunnableConfig] = None) -> dict:
    budget.set_cost_agent("ceo")  # attribute the CEO's model spend
    budget.set_cost_task(state.get("task_id"))
    model = _retry(_ceo_model().with_structured_output(CeoDecision))

    # When the user has approved the plan, forbid re-planning so the CEO actually
    # starts executing instead of proposing the plan again (the routing's
    # "plan first" rule otherwise tempts it back into another plan).
    approved = bool(state.get("plan_approved"))
    # CEO prompt + routing + the LIVE roster of specialists (from the registry,
    # so agents added/edited in the admin panel are immediately delegatable).
    system_text = (
        f"{registry.ceo_prompt()}\n\n{ROUTING_INSTRUCTIONS}\n\n{registry.roster_block()}"
    )
    if approved:
        system_text += (
            "\n\nIMPORTANT: The user has ALREADY APPROVED your plan. Do NOT use "
            "action='propose_plan' again. Begin executing now: respond with "
            "action='delegate' for the next step of the plan (or action='final' "
            "only if everything is already done)."
        )
    system = SystemMessage(content=system_text)

    thread_id = ""
    if config:
        thread_id = config.get("configurable", {}).get("thread_id", "")

    convo: list[BaseMessage] = list(state["messages"])
    findings = state.get("findings", [])
    if findings:
        convo = convo + [
            HumanMessage(
                content=(
                    "[Specialist results gathered so far for the current request]\n\n"
                    f"{_findings_block(findings)}\n\nDecide the next step."
                )
            )
        ]
    else:
        # First decision of the request: prime the CEO with relevant long-term
        # memory from the wiki (ephemeral — not persisted to chat history).
        context = _wiki_context(_last_user_text(convo))
        if context:
            convo = [HumanMessage(content=context)] + convo

    # Fold in anything the user sent WHILE the team has been working (they can
    # add to / tweak the current task without restarting it).
    additions = _drain_additions(thread_id) if thread_id else []
    if additions:
        convo = convo + [
            HumanMessage(
                content=(
                    "[The user sent these messages while the team was working. "
                    "Treat them as additions or changes to the CURRENT task and "
                    "incorporate them if relevant; ignore pure chit-chat. Do NOT "
                    "abandon the current task for an unrelated new request.]\n\n"
                    + "\n".join(f"- {a}" for a in additions)
                )
            )
        ]

    try:
        decision: CeoDecision = await model.ainvoke([system] + convo)
    except Exception:  # noqa: BLE001
        # The structured decision can fail when the model packs a long answer
        # into `final_answer` and runs past max_tokens, truncating the JSON
        # mid-string (invalid JSON -> pydantic ValidationError). Don't crash the
        # turn: fall back to a plain, non-structured final answer (valid text
        # even if long) built from whatever the team has gathered.
        logger.warning(
            "CEO structured decision failed; falling back to plain answer",
            exc_info=True,
        )
        answer = await _synthesize(state["messages"], findings)
        await _update_wiki_card(
            state.get("project_dir"), state["messages"], findings, answer
        )
        return {"messages": [AIMessage(content=answer)], "next_role": None}

    # Guard: if the plan is already approved but the model still tries to propose
    # another one, force it to commit to a step (one stricter retry).
    if approved and decision.action == "propose_plan":
        logger.info("CEO re-proposed after approval; forcing it to execute")
        strict = SystemMessage(
            content=system_text
            + "\n\nYou already proposed a plan and the user APPROVED it. You MUST "
            "NOT use action='propose_plan'. Respond with action='delegate' now."
        )
        try:
            decision = await model.ainvoke([strict] + convo)
        except Exception:  # noqa: BLE001
            answer = await _synthesize(state["messages"], findings)
            return {"messages": [AIMessage(content=answer)], "next_role": None}

    if decision.action == "delegate" and registry.is_specialist(decision.role):
        # Lock in ONE project folder for the whole request. Prefer an already
        # chosen folder, then the CEO's slug, then a thread-derived fallback so
        # every specialist on this request shares the same directory.
        project_dir = state.get("project_dir")
        if not project_dir:
            if decision.project:
                project_dir = sanitize_project(decision.project)
            else:
                project_dir = sanitize_project(f"project-{thread_id}" if thread_id else "")
        # Lock in the agreed file tree once, then reuse it for every builder.
        structure = state.get("structure") or (decision.structure or "").strip()
        # Track this request as a collab Task (created lazily on the first
        # delegation, so plan/clarify/small-talk turns make no task). Cheap, no LLM.
        task_id = state.get("task_id")
        if task_id is None:
            try:
                title = (_last_user_text(state["messages"]) or "task").strip()[:120]
                task_id = collab.create_task(title, created_by="ceo", owner="ceo")
            except Exception:  # noqa: BLE001 - tracking must never break a run
                logger.exception("failed to create collab task")
        # Record the CEO's reasoning as a "thought" — the Сознания stream.
        if task_id is not None and (decision.reasoning or decision.user_note):
            try:
                collab.record_event(
                    task_id, "ceo", "thought",
                    text=(decision.reasoning or "").strip(),
                    note=(decision.user_note or "").strip(),
                    to=decision.role,
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to record thought")
        return {
            "next_role": decision.role,
            "instruction": decision.instruction or "",
            "reasoning": decision.reasoning or "",
            "user_note": decision.user_note or "",
            "project_dir": project_dir,
            "structure": structure,
            "task_id": task_id,
        }

    if decision.action == "propose_plan":
        plan = (decision.plan or decision.final_answer or "").strip()
        if plan:
            return {
                "messages": [AIMessage(content=plan)],
                "next_role": None,
                "awaiting_input": True,
                "awaiting_kind": "plan",
            }
        # No plan text -> fall through to producing a normal answer.

    if decision.action == "clarify":
        question = (decision.question or decision.final_answer or "").strip()
        if question:
            return {
                "messages": [AIMessage(content=question)],
                "next_role": None,
                "awaiting_input": True,
                "awaiting_kind": "clarify",
            }
        # No question text -> fall through to producing a normal answer.

    answer = decision.final_answer or await _synthesize(state["messages"], findings)
    await _update_wiki_card(state.get("project_dir"), state["messages"], findings, answer)
    _mark_task_done(state)
    return {"messages": [AIMessage(content=answer)], "next_role": None}


async def _specialist_node(state: TeamState) -> dict:
    role = state["next_role"]
    instruction = state.get("instruction") or ""
    project = state.get("project_dir") or ""
    structure = (state.get("structure") or "").strip()
    task_id = state.get("task_id")
    label0 = registry.label(role)

    # Budget hard-stop: if this agent (or the company) is over a hard budget,
    # don't spend more — report it back so the CEO can wrap up honestly.
    if budget.blocked(role):
        g = budget.gate(role)
        try:
            activity.log("system", "budget_block", role, **g)
        except Exception:  # noqa: BLE001
            pass
        note = (f"[бюджет исчерпан] {label0}: лимит ${g['limit']:.2f} по scope "
                f"'{g['scope']}'/{g['window']} достигнут (потрачено ${g['spent']:.2f}). "
                "Задача не выполнена — поднимите лимит или дождитесь сброса окна.")
        return {
            "findings": state.get("findings", []) + [f"## {label0}\n{note}"],
            "steps": state.get("steps", 0) + 1,
            "next_role": None,
            "last_role": role,
        }
    budget.set_cost_task(task_id)
    task = (
        f"User's overall request:\n{_last_user_text(state['messages'])}\n\n"
        f"Your specific task from the CEO:\n{instruction}"
    )
    if structure and _uses_tools(role) and not _is_self_editor(role):
        task += (
            "\n\nAGREED PROJECT STRUCTURE — place files EXACTLY at these paths "
            "(relative to the project root); do not invent your own layout:\n"
            f"{structure}"
        )
    label = registry.label(role)

    # Delegation hand-off (stage 4). With negotiation on, the specialist consents
    # first and a decline is surfaced so the CEO re-routes; otherwise record the
    # auto-accepted hand-off for the task timeline. Both paths are best-effort.
    if task_id is not None:
        if settings.enable_negotiation:
            accepted, why = await collab.negotiate_delegation(
                task_id=task_id, from_agent="ceo", to_agent=role,
                task_text=task, reason=instruction, decider=_consent_decider,
            )
            if not accepted:
                note = f"[{label} declined the task] {why}".strip()
                return {
                    "findings": state.get("findings", []) + [f"## {label}\n{note}"],
                    "steps": state.get("steps", 0) + 1,
                    "next_role": None,
                    "last_role": role,
                }
        else:
            try:
                deleg_id = collab.open_delegation(task_id, "ceo", role, reason=instruction)
                collab.close_delegation(deleg_id, "accepted", actor=role, reason="auto-accepted")
            except Exception:  # noqa: BLE001
                logger.exception("failed to record delegation")

    result = await arun_specialist(role, task, project=project)

    # Record the outcome; if a builder saved nothing, open a help request and
    # suggest a helper so the CEO can route the fix through the normal loop.
    extra_finding = ""
    if task_id is not None:
        try:
            collab.record_event(task_id, role, "result", chars=len(result))
            if _uses_tools(role) and "no files were actually saved" in result:
                candidates = [s for s in registry.specialist_slugs() if _uses_tools(s)]
                helper = await collab.request_help(
                    task_id=task_id, requester=role,
                    summary=f"{role} produced no files for: {instruction[:120]}",
                    candidates=candidates, picker=_pick_helper,
                )
                if helper:
                    extra_finding = (
                        f"\n\n[help] {label} saved no files; consider delegating to "
                        f"{registry.label(helper)} ({helper}) to help."
                    )
        except Exception:  # noqa: BLE001
            logger.exception("failed to record result / open help")

    return {
        "findings": state.get("findings", []) + [f"## {label}\n{result}{extra_finding}"],
        "steps": state.get("steps", 0) + 1,
        "next_role": None,
        "last_role": role,
    }


async def _finalize_node(state: TeamState) -> dict:
    findings = state.get("findings", [])
    answer = await _synthesize(state["messages"], findings)
    await _update_wiki_card(state.get("project_dir"), state["messages"], findings, answer)
    _mark_task_done(state)
    return {"messages": [AIMessage(content=answer)], "next_role": None}


def _route(state: TeamState) -> str:
    if not state.get("next_role"):
        return END
    if state.get("steps", 0) >= MAX_STEPS:
        return "finalize"
    return "specialist"


def _build_graph(checkpointer) -> Any:
    graph = StateGraph(TeamState)
    graph.add_node("ceo", _ceo_node)
    graph.add_node("specialist", _specialist_node)
    graph.add_node("finalize", _finalize_node)

    graph.add_edge(START, "ceo")
    graph.add_conditional_edges(
        "ceo", _route, {"specialist": "specialist", "finalize": "finalize", END: END}
    )
    graph.add_edge("specialist", "ceo")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


_team_app: Any = None
_team_lock = asyncio.Lock()


async def _get_team_app() -> Any:
    """Build the compiled graph once, backed by a persistent SQLite checkpointer."""
    global _team_app
    if _team_app is None:
        async with _team_lock:
            if _team_app is None:
                db_dir = os.path.dirname(settings.db_path)
                if db_dir:
                    os.makedirs(db_dir, exist_ok=True)
                conn = await aiosqlite.connect(settings.db_path)
                saver = AsyncSqliteSaver(conn)
                await saver.setup()
                _team_app = _build_graph(saver)
    return _team_app


# --- live run registry (talk to the CEO while the team is working) ----------
#
# While a team run is in progress for a chat, the user can keep chatting with
# the CEO and add notes to the SAME task (the team never starts a second task
# mid-flight). These per-thread records let the quick-reply lane see what the
# team is doing, and let the running CEO pick up the user's mid-run additions at
# its next decision point. No locks needed: everything runs on one event loop.

_run_info: dict[str, dict[str, Any]] = {}


def _run_start(thread_id: str, task: str) -> None:
    _run_info[thread_id] = {
        "task": task,
        "status": "Анализирую задачу…",
        "additions": [],
    }


def _run_end(thread_id: str) -> None:
    _run_info.pop(thread_id, None)


def _set_status(thread_id: str, status: str) -> None:
    info = _run_info.get(thread_id)
    if info and status:
        info["status"] = status


def _record_addition(thread_id: str, text: str) -> None:
    info = _run_info.get(thread_id)
    if info is not None and text:
        info["additions"].append(text)


def _drain_additions(thread_id: str) -> list[str]:
    info = _run_info.get(thread_id)
    if not info or not info["additions"]:
        return []
    items = info["additions"]
    info["additions"] = []
    return items


async def aquick_reply(text: str, thread_id: str) -> str:
    """A lightweight CEO reply for a message sent WHILE the team is busy.

    The team finishes one task at a time and never switches mid-flight. This lane
    lets the user keep talking to the CEO and append notes/changes to the CURRENT
    task: the message is recorded so the running team folds it in at its next
    step. Returns a short reply in the user's language. Does NOT run the full team
    or touch the checkpointer."""
    info = _run_info.get(thread_id) or {}
    task = info.get("task", "(a task is currently in progress)")
    status = info.get("status", "В работе…")

    # Record it so the running CEO can incorporate it as an addition next step.
    _record_addition(thread_id, text)

    system = SystemMessage(
        content=(
            f"{registry.ceo_prompt()}\n\nSITUATION: your team is RIGHT NOW working on a task "
            "in the background. The user just sent a message mid-work. You CANNOT "
            "start a new task or interrupt the current one — the team finishes one "
            "task at a time. Reply briefly (1-3 sentences) in the user's language. "
            "If the user is adding or changing something, confirm you've noted it "
            "and will fold it into the current work. If they ask about progress, "
            "tell them the current status. If they ask for something unrelated, "
            "say you'll handle it once the current task is finished. Do NOT write "
            "code here."
        )
    )
    human = HumanMessage(
        content=(
            f"Current task in progress:\n{task}\n\n"
            f"What the team is doing right now:\n{status}\n\n"
            f"User's new message:\n{text}"
        )
    )
    try:
        resp = await _retry(_ceo_model()).ainvoke([system, human])
        return _content_to_text(resp.content) or "Принял — учту в текущей задаче."
    except Exception:  # noqa: BLE001
        return "Принял ваше сообщение — учту в текущей задаче."


# --- public API -------------------------------------------------------------

async def arun_team(
    text: str,
    thread_id: str,
    on_event: Optional[Callable[[str, str], Awaitable[None]]] = None,
    plan_approved: bool = False,
) -> tuple[str, Optional[str], bool]:
    """Run one turn through the CEO-led team for the given chat thread.

    If ``on_event`` is given, the team's progress is streamed to it as it
    happens. It's called as ``on_event(kind, text)`` where ``kind`` is
    ``"delegate"`` (a short one-line "the CEO is now pinging <role> to do X",
    meant to accumulate in one live message) or ``"result"`` (a specialist's
    finished reply, meant to be sent as its own message).

    Returns ``(answer, awaiting_kind, did_work)`` where ``awaiting_kind`` is
    ``"plan"`` when the CEO is waiting for plan approval, ``"clarify"`` when it
    asked a clarifying question, or ``None`` when the answer is final. ``did_work``
    is True when at least one specialist ran (so the caller can announce
    completion only for real work, not small talk).
    """
    app = await _get_team_app()

    # Company-wide budget hard-stop: refuse to start a run when over budget.
    if budget.blocked(None):
        g = budget.gate(None)
        try:
            activity.log("system", "budget_block", "company", **g)
        except Exception:  # noqa: BLE001
            pass
        msg = (f"🚫 Бюджет компании исчерпан: лимит ${g['limit']:.2f}/{g['window']} "
               f"достигнут (потрачено ${g['spent']:.2f}). Поднимите лимит в админ-"
               "панели или дождитесь сброса окна.")
        return msg, None, False

    inputs = {
        "messages": [HumanMessage(content=text)],
        "findings": [],  # reset scratchpad for each new request
        "next_role": None,
        "instruction": None,
        "reasoning": None,
        "user_note": None,
        "last_role": None,
        "awaiting_input": False,  # reset each run
        "awaiting_kind": None,  # reset each run
        "plan_approved": plan_approved,  # set when resuming after plan approval
        "task_id": None,  # collab task created on the first delegation (stage 4)
        "steps": 0,
    }
    # Each internal step is two graph super-steps (ceo -> specialist), so the
    # LangGraph cap must sit above 2*MAX_STEPS or the run trips GraphRecursionError
    # ("needs more steps") before our own _finalize at MAX_STEPS can answer.
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": MAX_STEPS * 2 + 6}

    # Register the run so the user can chat with the CEO / add to the task while
    # it's in progress (see aquick_reply). Always cleared when the run ends.
    _run_start(thread_id, text)
    try:
        if on_event is None:
            result = await app.ainvoke(inputs, config=config)
            messages = result.get("messages", [])
            answer = _content_to_text(messages[-1].content) if messages else "..."
            if settings.translate_chatter:
                answer = await aensure_russian(answer)
            return answer, result.get("awaiting_kind"), bool(result.get("steps"))

        translate = settings.translate_chatter

        final_answer = "..."
        awaiting_kind: Optional[str] = None
        did_work = False
        async for chunk in app.astream(inputs, config=config, stream_mode="updates"):
            for node, delta in chunk.items():
                if not isinstance(delta, dict):
                    continue
                if delta.get("awaiting_kind"):
                    awaiting_kind = delta["awaiting_kind"]
                messages = delta.get("messages")
                if messages:
                    final_answer = _content_to_text(messages[-1].content) or final_answer
                if node == "ceo" and delta.get("next_role"):
                    role = delta["next_role"]
                    label = registry.label(role)
                    # `user_note` is already in the user's language; prefer it.
                    note = (delta.get("user_note") or "").strip()
                    if not note:
                        note = (delta.get("reasoning") or "").strip()
                        if translate and note:
                            note = await atranslate_ru(note)
                    _set_status(thread_id, note or f"Подключаю: {label}")
                    # One short line meant to accumulate in the live progress msg.
                    line = f"🧭 {label} — {note}" if note else f"🧭 {label}"
                    await on_event("delegate", line)
                elif node == "specialist":
                    findings = delta.get("findings")
                    if findings:
                        did_work = True
                        role = delta.get("last_role")
                        result = findings[-1]
                        if "\n" in result:
                            result = result.split("\n", 1)[1]  # drop the "## label"
                        label = registry.label(role) if role else ""
                        _set_status(thread_id, f"{label}: результат получен")
                        if translate:
                            result = await atranslate_ru(result)
                        head = f"✅ *{label} — готово*\n" if label else "✅ "
                        await on_event("result", f"{head}{result}")
        if settings.translate_chatter:
            # Guarantee the user-facing plan / question / final answer is Russian
            # even when the CEO model slips into English.
            final_answer = await aensure_russian(final_answer)
        return final_answer, awaiting_kind, did_work
    finally:
        _run_end(thread_id)


async def arun_specialist(role: str, text: str, project: str = "") -> str:
    """Ask a single specialist directly (stateless, no team orchestration).

    ``project`` points the file tools at one shared ``<workspace>/<project>``
    folder so every specialist on the same request writes into ONE directory.
    """
    budget.set_cost_agent(role)  # attribute this specialist's model spend
    if _uses_tools(role):
        self_edit = settings.enable_self_modify and _is_self_editor(role)
        worktree: Optional[dict] = None
        if self_edit:
            # Governance: ask the human before the bot edits its OWN code.
            if approvals.has_asker():
                ok = await approvals.request_approval(
                    "self_modify",
                    f"{registry.label(role)} собирается изменить собственный код бота",
                    agent=role,
                )
                if not ok:
                    return "🛑 Изменение собственного кода отклонено пользователем."
            # Isolate the edit in a dedicated git worktree on a fresh branch so the
            # live bot is never disturbed (falls back to in-place if unavailable).
            if settings.self_worktree:
                worktree = await asyncio.to_thread(selfmod.create_worktree, role)
                if worktree:
                    text = (
                        f"[ИЗОЛИРОВАННЫЙ WORKTREE] Ты уже на свежей ветке "
                        f"`{worktree['branch']}` в отдельном git worktree — это твой "
                        "корень репозитория. НЕ создавай новую ветку: правь файлы, "
                        "запускай тесты и отчитайся диффом.\n\n" + text
                    )
        set_project_subdir(project)
        set_current_agent(role)  # so @requires can check this agent's permissions
        # Point tools at the worktree (isolated) or the live repo (fallback).
        set_self_edit(self_edit, root=(worktree["path"] if worktree else ""))
        try:
            result = await _tool_agent(role).ainvoke(
                {"messages": [HumanMessage(content=text)]},
                # Allow many tool calls so multi-file work isn't cut short.
                config={"recursion_limit": 60},
            )
        finally:
            # Always clear this run's identity/root, even on error — otherwise the
            # next call in the same async context inherits the wrong agent, project
            # folder or self-edit root (breaks @requires audit + file sandboxing).
            set_self_edit(False, root="")
            set_current_agent("")
            set_project_subdir("")
        reply = _content_to_text(result["messages"][-1].content) or "..."
        # In self-edit mode the "project" is the whole repo, so don't dump every
        # file — the maintainer's own git diff / report is the ground truth.
        if self_edit:
            if worktree:
                stat = await asyncio.to_thread(selfmod.diffstat, worktree["path"])
                reply += (f"\n\n[worktree] ветка `{worktree['branch']}`\n"
                          f"путь: {worktree['path']}\n{stat}")
            try:
                activity.log(role, "self_modify_run",
                             worktree["branch"] if worktree else "in-place")
            except Exception:  # noqa: BLE001
                pass
            return reply
        # Ground the report in reality: append what is ACTUALLY on disk so the
        # CEO sees the true file set, not just what the specialist claimed.
        try:
            saved = current_project_files()
        except Exception:  # noqa: BLE001
            saved = []
        listing = "\n".join(saved) if saved else "(no files were actually saved!)"
        reply += f"\n\n[Files actually on disk in the project folder now]\n{listing}"
        return reply

    # Non-tool specialists (analysts/designer) can't call tools, so prime them
    # with relevant shared memory read-only — they still benefit from what the
    # team already knows.
    prompt = registry.prompt(role)
    memory = _wiki_context(text)
    system_text = f"{prompt}\n\n{memory}" if memory else prompt
    response = await _retry(_specialist_model(role)).ainvoke(
        [SystemMessage(content=system_text), HumanMessage(content=text)]
    )
    return _content_to_text(response.content) or "..."


async def aagent_reply(
    slug: str, text: str, history: Optional[list[tuple[str, str]]] = None
) -> str:
    """A conversational reply from one agent in its OWN Telegram bot (a personal
    DM). Uses the agent's prompt + recent history; no orchestration, no file/shell
    tools — it's a chat with that specialist. Output is in the user's language."""
    messages: list[BaseMessage] = [SystemMessage(content=registry.prompt(slug))]
    for who, content in history or []:
        messages.append(
            HumanMessage(content=content) if who == "user" else AIMessage(content=content)
        )
    messages.append(HumanMessage(content=text))
    resp = await _retry(_specialist_model(slug)).ainvoke(messages)
    reply = _content_to_text(resp.content) or "..."
    if settings.translate_chatter:
        reply = await aensure_russian(reply)
    return reply


# --- group chat presence (live multi-human group) ---------------------------

class GroupTurn(BaseModel):
    """Who, if anyone, should naturally respond next in the team group chat."""

    speak: bool = Field(description="True if exactly one teammate should respond now.")
    slug: Optional[str] = Field(
        default=None, description="The teammate's key (from the roster), or null for silence."
    )
    reason: str = Field(default="", description="One short sentence: why this teammate, or why silence.")


async def aroute_group_speaker(
    transcript: str, roster: list[tuple[str, str, str]], last_speaker: Optional[str]
) -> tuple[Optional[str], str]:
    """Pick AT MOST ONE teammate to respond to the latest group message, or None.

    Biased hard toward silence — a wall of replies is the failure mode. One small
    structured LLM call. ``roster`` is (slug, name, role) of agents present."""
    if not roster:
        return None, "no agents present"
    lines = "\n".join(f"- {slug} — {name} ({role})" for slug, name, role in roster)
    valid = {slug for slug, _n, _r in roster}
    system = SystemMessage(
        content=(
            "You decide who, if anyone, naturally responds next in a work team's "
            "group chat (real people + AI teammates). Read the LATEST message and "
            "pick the ONE teammate who would naturally reply right now — or choose "
            "silence. Reply when a teammate is addressed or greeted, a question is "
            "asked (even without a '?'), help is offered, or someone can add genuine "
            "value or a short bit of banter. Stay silent for filler ('ок', 'да', "
            "'лол', reactions) or when nobody really has anything to add. Pick AT "
            "MOST ONE — never make everyone answer at once. A greeting or a direct "
            "question usually deserves a brief reply; lean toward being a responsive, "
            "natural colleague rather than over-cautious.\n\n"
            f"Teammates present (use the exact key on the left):\n{lines}"
            + (f"\n\nThe teammate who just spoke is '{last_speaker}' — do not pick them."
               if last_speaker else "")
        )
    )
    human = HumanMessage(
        content=(
            "Recent group conversation (oldest first; the LAST line is the new "
            f"message):\n{transcript}\n\nShould exactly one teammate respond now? "
            "If yes, who (slug)? If in doubt, choose silence."
        )
    )
    try:
        model = _retry(_ceo_model().with_structured_output(GroupTurn))
        decision: GroupTurn = await model.ainvoke([system, human])
    except Exception:  # noqa: BLE001 - a routing hiccup means stay silent
        logger.warning("group routing failed; staying silent", exc_info=True)
        return None, "routing error"
    if not decision.speak or not decision.slug:
        return None, decision.reason or "silence"
    slug = decision.slug.strip()
    if slug not in valid or slug == last_speaker:
        return None, "invalid/just-spoke"
    return slug, decision.reason or ""


class GroupDecision(BaseModel):
    """One agent's OWN call on whether to chime into the group right now."""

    respond: bool = Field(description="True if YOU would naturally reply to the latest message now.")
    reply: str = Field(default="", description="Your brief, natural group message (only if respond=True).")


async def agroup_decide(slug: str, transcript: str) -> tuple[bool, str]:
    """Each agent decides FOR ITSELF (its own model call) whether to respond, and
    what to say. This is the fully-independent mode: every present agent runs this
    in parallel and those who opt in reply. Returns (respond, reply_text)."""
    label = registry.label(slug)
    system = SystemMessage(
        content=(
            f"{registry.prompt(slug)}\n\nYou are {label} in your team's GROUP CHAT "
            "with real people and other AI teammates. Decide FOR YOURSELF whether "
            "YOU would naturally respond to the LATEST message right now. Reply only "
            "if it's addressed to you, it's your area/expertise, the room was greeted, "
            "or you genuinely have something useful or a short natural remark to add. "
            "It is completely fine — and often right — to STAY SILENT and let "
            "teammates answer; not everyone should reply to everything. If you reply, "
            "keep it brief (1-2 sentences), natural, in the user's language; you may "
            "address people or teammates by name."
        )
    )
    human = HumanMessage(
        content=f"Recent group conversation (last line is newest):\n{transcript}\n\n"
        "Do you respond? If yes, what exactly do you say?"
    )
    try:
        model = _retry(_specialist_model(slug).with_structured_output(GroupDecision))
        decision: GroupDecision = await model.ainvoke([system, human])
    except Exception:  # noqa: BLE001 - a hiccup means this agent simply stays silent
        logger.warning("group decision failed for %s", slug, exc_info=True)
        return False, ""
    reply = (decision.reply or "").strip()
    if not decision.respond or not reply:
        return False, ""
    if settings.translate_chatter:
        reply = await aensure_russian(reply)
    return True, reply


async def agroup_reply(
    slug: str, transcript: str, *, work_intent: bool, project: str
) -> str:
    """Generate ONE teammate's group-chat message. Conversational by default; when
    the moment calls for real work AND the agent has the tools/permissions, it may
    actually create or change files in a shared group project folder."""
    label = registry.label(slug)
    if work_intent and _uses_tools(slug):
        set_project_subdir(project)
        set_current_agent(slug)
        task = (
            f"You are {label}, chatting in the team's group. Recent conversation:\n"
            f"{transcript}\n\nDo the concrete work that's being asked of you (create/"
            "edit the needed files with your tools), then reply to the group in 1-3 "
            "short sentences saying what you did and the file paths — natural, like a "
            "colleague. Do not paste large code into the chat."
        )
        try:
            # Enough tool steps to actually finish a real work request (configurable),
            # while still bounding a runaway fan-out.
            result = await _tool_agent(slug).ainvoke(
                {"messages": [HumanMessage(content=task)]},
                config={"recursion_limit": settings.group_work_max_steps},
            )
            reply = _content_to_text(result["messages"][-1].content) or "Готово."
        except Exception:  # noqa: BLE001
            logger.exception("group tool reply failed for %s", slug)
            reply = "Не получилось доделать — гляну ещё раз."
        finally:
            # Don't leak this agent's identity/folder into the next group reply.
            set_current_agent("")
            set_project_subdir("")
        if settings.translate_chatter:
            reply = await aensure_russian(reply)
        return reply

    system = SystemMessage(
        content=(
            f"{registry.prompt(slug)}\n\nYou are {label} in your team's GROUP CHAT "
            "with real people and other AI teammates. Reply naturally and briefly "
            "(1-3 sentences), like a colleague — not a formal report. You may address "
            "people or teammates by name. If you have nothing useful to add, say so in "
            "a few words. Answer in the user's language."
        )
    )
    human = HumanMessage(
        content=f"Recent group conversation (the last line is newest):\n{transcript}\n\n"
        f"Write {label}'s next message in the group."
    )
    try:
        resp = await _retry(_specialist_model(slug)).ainvoke([system, human])
        reply = _content_to_text(resp.content) or "..."
    except Exception:  # noqa: BLE001
        logger.exception("group reply failed for %s", slug)
        return ""
    if settings.translate_chatter:
        reply = await aensure_russian(reply)
    return reply
