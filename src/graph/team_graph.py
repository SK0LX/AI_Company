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

from src import quota
from src.registry import registry
from src.agents.tools import (
    current_project_files,
    sanitize_project,
    set_current_agent,
    set_project_subdir,
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
    steps: int


# --- models -----------------------------------------------------------------

def _make_model(model_name: str) -> BaseChatModel:
    """Build a chat model for the configured provider."""
    if settings.llm_provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=settings.google_api_key,
            max_output_tokens=settings.max_tokens,
            temperature=settings.temperature,
            # The SDK retries internally too; keep it low so a hard quota 429
            # fails fast instead of stacking with our own _retry wrapper.
            max_retries=1,
        )
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model_name,
            api_key=settings.anthropic_api_key,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            max_retries=1,
        )
    if settings.llm_provider == "openrouter":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_name,
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            max_retries=1,
            # Count every call against the OpenRouter free daily cap so we can
            # warn the user before they hit it.
            callbacks=[quota.counter],
            # Optional OpenRouter attribution headers (used for their rankings).
            default_headers={
                "HTTP-Referer": "https://github.com/ai-it-company",
                "X-Title": "AI IT Company",
            },
        )
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")


def _retry(runnable: Runnable) -> Runnable:
    """Add exponential backoff so transient rate limits don't fail a turn."""
    return runnable.with_retry(
        retry_if_exception_type=_RETRYABLE,
        stop_after_attempt=3,
        wait_exponential_jitter=True,
    )


@lru_cache(maxsize=12)
def _model_by_name(model_name: str) -> BaseChatModel:
    return _make_model(model_name)


def _ceo_model() -> BaseChatModel:
    return _model_by_name(registry.model_for("ceo") or settings.ceo_model_resolved)


def _specialist_model(slug: Optional[str] = None) -> BaseChatModel:
    """Model for a specialist. Honors the agent's per-agent `model` from the
    registry; falls back to the global default."""
    name = (registry.model_for(slug) if slug else "") or settings.agent_model_resolved
    return _model_by_name(name)


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
    role, or a custom agent granted file/shell permissions in the panel."""
    return (
        role in _FILE_TOOL_ROLES
        or _perm(role, "can_edit_files")
        or _perm(role, "can_run_shell")
    )


def _tool_agent(role: str):
    """Build a ReAct agent for a tool-using specialist. NOT cached, so prompt and
    permission edits made in the admin panel take effect on the next run. Tools
    follow built-in role behavior, plus shell is gated by the `can_run_shell`
    permission."""
    from langgraph.prebuilt import create_react_agent

    from src.agents.tools import (
        delete_file,
        list_files,
        move_file,
        read_file,
        run_python,
        run_shell,
        write_file,
    )

    base_prompt = registry.prompt(role)
    model = _specialist_model(role)
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
        tools: list = [list_files, read_file, write_file]
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
        tools = [list_files, read_file, write_file]
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
        tools = [list_files, read_file, write_file, move_file, delete_file]
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

    # Default: builders (developer/frontend) and custom agents with file perms.
    tools = [write_file, read_file, list_files]
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
    """Write/update a concise project card in the wiki after a task finishes.

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
                "Update the team's wiki card for this project. Output ONLY the "
                "full Markdown note (no preamble). Keep it concise. Use this "
                "structure:\n"
                f"# {slug}\n\n## Summary\n(1-3 sentences)\n\n## Key decisions\n"
                "(bullets)\n\n## Structure / files\n(bullets of the main files "
                "and folders)\n\n## Status\n(one line)\n\n"
                f"Existing card:\n{existing}\n\n"
                f"User request:\n{_last_user_text(messages)}\n\n"
                f"Specialist results:\n{_findings_block(findings)}\n\n"
                f"Final answer delivered to the user:\n{answer}"
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
        return {
            "next_role": decision.role,
            "instruction": decision.instruction or "",
            "reasoning": decision.reasoning or "",
            "user_note": decision.user_note or "",
            "project_dir": project_dir,
            "structure": structure,
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
    return {"messages": [AIMessage(content=answer)], "next_role": None}


async def _specialist_node(state: TeamState) -> dict:
    role = state["next_role"]
    instruction = state.get("instruction") or ""
    project = state.get("project_dir") or ""
    structure = (state.get("structure") or "").strip()
    task = (
        f"User's overall request:\n{_last_user_text(state['messages'])}\n\n"
        f"Your specific task from the CEO:\n{instruction}"
    )
    if structure and _uses_tools(role):
        task += (
            "\n\nAGREED PROJECT STRUCTURE — place files EXACTLY at these paths "
            "(relative to the project root); do not invent your own layout:\n"
            f"{structure}"
        )
    result = await arun_specialist(role, task, project=project)
    label = registry.label(role)
    return {
        "findings": state.get("findings", []) + [f"## {label}\n{result}"],
        "steps": state.get("steps", 0) + 1,
        "next_role": None,
        "last_role": role,
    }


async def _finalize_node(state: TeamState) -> dict:
    findings = state.get("findings", [])
    answer = await _synthesize(state["messages"], findings)
    await _update_wiki_card(state.get("project_dir"), state["messages"], findings, answer)
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
        "steps": 0,
    }
    config = {"configurable": {"thread_id": thread_id}}

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
    if _uses_tools(role):
        set_project_subdir(project)
        set_current_agent(role)  # so @requires can check this agent's permissions
        result = await _tool_agent(role).ainvoke(
            {"messages": [HumanMessage(content=text)]},
            # Allow many tool calls so multi-file work isn't cut short.
            config={"recursion_limit": 60},
        )
        reply = _content_to_text(result["messages"][-1].content) or "..."
        # Ground the report in reality: append what is ACTUALLY on disk so the
        # CEO sees the true file set, not just what the specialist claimed.
        try:
            saved = current_project_files()
        except Exception:  # noqa: BLE001
            saved = []
        listing = "\n".join(saved) if saved else "(no files were actually saved!)"
        reply += f"\n\n[Files actually on disk in the project folder now]\n{listing}"
        return reply

    prompt = registry.prompt(role)
    response = await _retry(_specialist_model(role)).ainvoke(
        [SystemMessage(content=prompt), HumanMessage(content=text)]
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
