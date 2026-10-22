"""Run a read-only local Cursor agent and print its review."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

# Read and search only. The review must not edit the checkout.
READ_ONLY_TOOLS = ("read", "grep", "glob", "ls")

EXIT_OK = 0
EXIT_STARTUP = 1
EXIT_RUN_FAILED = 2


def run_review(
    prompt: str,
    *,
    cwd: str,
    model: str,
    api_key: str,
    prompter: Callable[..., Any] | None = None,
) -> int:
    """Send ``prompt`` to a one-shot local agent and write the review to stdout.

    Returns 0 when the run finishes, 1 when the agent never starts, and 2 when
    the run starts but does not finish.
    """
    if not api_key.strip():
        print(
            "Set CURSOR_API_KEY to a user or service-account API key from the Cursor dashboard.",
            file=sys.stderr,
        )
        return EXIT_STARTUP

    send = prompter or _agent_prompt
    try:
        result = send(prompt, cwd=cwd, model=model, api_key=api_key)
    except Exception as err:
        if _is_cursor_agent_error(err):
            request_id = getattr(err, "request_id", None)
            print(
                f"startup failed: {err}, retryable={getattr(err, 'is_retryable', False)}"
                + (f", request_id={request_id}" if request_id else ""),
                file=sys.stderr,
            )
            return EXIT_STARTUP
        raise

    print(
        f"agent_id={result.agent_id} run_id={result.id} "
        f"status={result.status} duration_ms={result.duration_ms}",
        file=sys.stderr,
    )

    if result.status != "finished":
        message = result.result or getattr(result, "error_message", "") or ""
        if message:
            print(message, file=sys.stderr)
        return EXIT_RUN_FAILED

    text = result.result or ""
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    return EXIT_OK


def _agent_prompt(prompt: str, *, cwd: str, model: str, api_key: str) -> Any:
    from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

    # Agent.prompt creates the agent, waits, and disposes. Local is explicit so
    # a missing cloud config cannot silently fall through to the wrong runtime.
    return Agent.prompt(
        prompt,
        AgentOptions(
            api_key=api_key,
            model=model,
            name="pr-review",
            tools=list(READ_ONLY_TOOLS),
            local=LocalAgentOptions(cwd=cwd, setting_sources=["project"]),
        ),
    )


def _is_cursor_agent_error(err: BaseException) -> bool:
    try:
        from cursor_sdk import CursorAgentError
    except ImportError:
        return err.__class__.__name__ == "CursorAgentError"
    return isinstance(err, CursorAgentError)
