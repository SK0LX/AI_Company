"""Bridge to the Claude Code CLI (`claude -p`).

Runs a task through Claude's OWN agent loop — it plans, spawns subagents (your
team personas, passed via --agents), reads/writes files and runs commands in a
project dir — and streams each step back so you can watch the delegation live
(like the office step ticker). The final answer + session id (for follow-ups) +
cost are returned.

Auth is whatever `claude` is logged into where this runs: a subscription
(`claude login`) or ANTHROPIC_API_KEY in the environment. The bridge is identical
either way — it just shells out to the official CLI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# on_step(kind, text): kind ∈ {"think","tool","delegate","result"}
StepCb = Callable[[str, str], Awaitable[None]]

# Your team as Claude Code subagents — the lead Claude delegates to them by name,
# so the "team works and delegates" feel is preserved while Claude does the work.
TEAM_AGENTS: dict[str, dict] = {
    "analyst": {
        "description": "Аналитик: собирает требования, проектирует структуру и контракты API. Вызывай его первым на новой задаче.",
        "prompt": "Ты — бизнес/системный аналитик команды. Кратко собери требования, "
                  "опиши структуру и контракты (endpoints, модель данных). Не пиши прод-код — "
                  "ты передаёшь чёткое ТЗ разработчику.",
    },
    "developer": {
        "description": "Backend-разработчик: пишет серверный код, движки, API, тесты к ним.",
        "prompt": "Ты — backend-разработчик. Пиши рабочий серверный код по ТЗ аналитика, "
                  "аккуратно и с запуском/проверкой. Отчитайся файлами, что сделал.",
    },
    "frontend": {
        "description": "Frontend-разработчик: вёрстка и клиентская логика (HTML/CSS/JS).",
        "prompt": "Ты — frontend-разработчик. Делай интерфейс и клиентскую логику, "
                  "подключайся к API бэкенда. Отчитайся файлами.",
    },
    "tester": {
        "description": "Тестировщик/QA: пишет и гоняет тесты, ищет баги, проверяет сборку.",
        "prompt": "Ты — тестировщик. Проверь результат, напиши/запусти тесты, "
                  "найди проблемы и кратко отчитайся, что работает, а что нет.",
    },
}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


async def run_claude(
    prompt: str,
    *,
    cwd: str,
    resume: Optional[str] = None,
    agents: Optional[dict] = None,
    model: Optional[str] = None,
    permission_mode: str = "acceptEdits",
    use_subscription: bool = False,
    on_step: Optional[StepCb] = None,
    can_use_tool: Optional[Callable] = None,
    no_tools: bool = False,
    timeout: float = 1200.0,
) -> dict:
    """Run one task via `claude -p` (stream-json) and return
    {ok, answer, session_id, cost_usd, error}. Calls on_step(kind, text) live.

    ``use_subscription``: claude prefers ANTHROPIC_API_KEY over a logged-in
    subscription whenever the env var is present. Set this to drop the key from the
    subprocess so it falls back to the `claude login` credentials (the cheap path).

    ``can_use_tool``: a Claude-Agent-SDK permission callback (see src/claude_perms).
    When given, the task runs through the in-process Agent SDK instead of the raw
    CLI so every tool use passes the 4-category permission gate (auto / web-ask).

    ``no_tools``: disable ALL tools (--disallowedTools). For pure text decisions —
    the lead router, relay hop, plan, summary — so the model can't waste seconds
    attempting (and getting denied) file/web tools; it just reasons and replies fast.
    """
    if can_use_tool is not None:
        return await run_claude_sdk(
            prompt, cwd=cwd, resume=resume, agents=agents, model=model,
            use_subscription=use_subscription, on_step=on_step,
            can_use_tool=can_use_tool, timeout=timeout,
        )
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--permission-mode", permission_mode]
    if agents:
        cmd += ["--agents", json.dumps(agents, ensure_ascii=False)]
    if resume:
        cmd += ["--resume", resume]
    if model:
        cmd += ["--model", model]
    if no_tools:
        # append LAST: --disallowedTools is variadic and would swallow later flags
        cmd += ["--disallowedTools", "Bash", "Edit", "Write", "Read", "Glob",
                "Grep", "WebFetch", "WebSearch", "Task", "NotebookEdit",
                "MultiEdit", "LS", "TodoWrite"]

    env = {k: v for k, v in os.environ.items()
           if not (use_subscription and k == "ANTHROPIC_API_KEY")}
    if no_tools:
        # These are quick decisions — extended thinking roughly doubles their wall
        # time (haiku "thinks" 10-20s for a one-line answer). Turn it off for speed.
        env["MAX_THINKING_TOKENS"] = "0"
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    answer, session_id, cost, ok, err = "", None, 0.0, False, ""

    async def _emit(kind: str, text: str) -> None:
        if on_step and text:
            try:
                await on_step(kind, text)
            except Exception:  # noqa: BLE001
                pass

    async def _read() -> None:
        nonlocal answer, session_id, cost, ok, err
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                session_id = ev.get("session_id")
            elif t == "assistant":
                for block in (ev.get("message", {}).get("content") or []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text", "").strip():
                        await _emit("think", block["text"].strip())
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "tool")
                        ti = block.get("input") or {}
                        if name in ("Agent", "Task"):  # delegation to a subagent
                            who = ti.get("subagent_type") or ti.get("description") or "субагент"
                            what = ti.get("description") or ti.get("prompt") or ""
                            await _emit("delegate", f"↪️ {who}: {str(what)[:80]}")
                        else:
                            hint = ti.get("file_path") or ti.get("path") or ti.get("command") or ti.get("pattern") or ""
                            await _emit("tool", f"🔧 {name} {str(hint)[:60]}".strip())
            elif t == "result":
                ok = not ev.get("is_error")
                answer = ev.get("result") or answer
                cost = float(ev.get("total_cost_usd") or 0.0)
                session_id = ev.get("session_id") or session_id
                await _emit("result", answer)

    try:
        await asyncio.wait_for(_read(), timeout=timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        err = f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    if not ok and not err:
        err = (await proc.stderr.read()).decode("utf-8", "replace")[:300] if proc.stderr else "failed"
    return {"ok": ok, "answer": answer, "session_id": session_id, "cost_usd": cost, "error": err}


async def run_claude_sdk(
    prompt: str,
    *,
    cwd: str,
    resume: Optional[str] = None,
    agents: Optional[dict] = None,
    model: Optional[str] = None,
    use_subscription: bool = False,
    on_step: Optional[StepCb] = None,
    can_use_tool: Optional[Callable] = None,
    timeout: float = 1200.0,
) -> dict:
    """Same contract as :func:`run_claude`, but runs the task IN-PROCESS through the
    Claude Agent SDK so a ``can_use_tool`` callback governs every tool use (the
    4-category permission model). Returns {ok, answer, session_id, cost_usd, error}
    and streams on_step(kind, text) live, exactly like the CLI path."""
    from claude_agent_sdk import (
        AgentDefinition, AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
        ResultMessage, SystemMessage, TextBlock, ToolUseBlock,
    )

    sdk_agents = None
    if agents:
        sdk_agents = {
            name: AgentDefinition(description=a.get("description", ""), prompt=a.get("prompt", ""))
            for name, a in agents.items()
        }
    # The SDK MERGES options.env over the inherited environment, so omitting a key
    # doesn't remove it. To force the subscription login (claude prefers
    # ANTHROPIC_API_KEY when present), explicitly BLANK the key — empty is falsy, so
    # claude falls back to `claude login`.
    env = dict(os.environ)
    if use_subscription:
        env["ANTHROPIC_API_KEY"] = ""
    options = ClaudeAgentOptions(
        cwd=cwd, model=(model or None), resume=resume, env=env,
        agents=sdk_agents, can_use_tool=can_use_tool,
        permission_mode="default",  # every gated tool routes to can_use_tool
    )
    answer, session_id, cost, ok, err = "", None, 0.0, False, ""

    async def _emit(kind: str, text: str) -> None:
        if on_step and text:
            try:
                await on_step(kind, text)
            except Exception:  # noqa: BLE001
                pass

    async def _run() -> None:
        nonlocal answer, session_id, cost, ok, err
        # can_use_tool requires streaming mode -> use the client (query() needs a
        # plain string but rejects callbacks); the client accepts a string prompt.
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage):
                    session_id = getattr(msg, "session_id", None) or session_id
                elif isinstance(msg, AssistantMessage):
                    for block in (msg.content or []):
                        if isinstance(block, TextBlock) and block.text.strip():
                            await _emit("think", block.text.strip())
                        elif isinstance(block, ToolUseBlock):
                            name, ti = block.name, (block.input or {})
                            if name in ("Agent", "Task"):
                                who = ti.get("subagent_type") or ti.get("description") or "субагент"
                                what = ti.get("description") or ti.get("prompt") or ""
                                await _emit("delegate", f"↪️ {who}: {str(what)[:80]}")
                            else:
                                hint = ti.get("file_path") or ti.get("path") or ti.get("command") or ti.get("pattern") or ti.get("url") or ""
                                await _emit("tool", f"🔧 {name} {str(hint)[:60]}".strip())
                elif isinstance(msg, ResultMessage):
                    ok = not msg.is_error
                    answer = msg.result or answer
                    cost = float(msg.total_cost_usd or 0.0)
                    session_id = msg.session_id or session_id
                    await _emit("result", answer)

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        err = f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        logger.exception("claude SDK run failed")
    return {"ok": ok, "answer": answer, "session_id": session_id, "cost_usd": cost, "error": err}
