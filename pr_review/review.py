"""Send a pull request review to the OpenAdapter chat completions API."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_URL = "https://api.openadapter.in/v1"
DEFAULT_MODEL = "GLM-5"
USER_AGENT = "pr-review-agent/0.1"

EXIT_OK = 0
EXIT_STARTUP = 1
EXIT_RUN_FAILED = 2


@dataclass(frozen=True)
class ReviewOutcome:
    """Result of one review run.

    ``text`` is the review markdown when ``exit_code`` is 0. On failure it is a
    short reason, not a verdict.
    """

    exit_code: int
    text: str = ""
    run_id: str | None = None
    status: str | None = None


def chat_completions_url() -> str:
    """Return the chat completions URL from ``OPENADAPTOR_BASE_URL``."""
    base = os.environ.get("OPENADAPTOR_BASE_URL", "").strip() or DEFAULT_BASE_URL
    return base.rstrip("/") + "/chat/completions"


def model_name() -> str:
    """Return the model id from ``OPENADAPTOR_MODEL``."""
    return os.environ.get("OPENADAPTOR_MODEL", "").strip() or DEFAULT_MODEL


def run_review(
    prompt: str,
    *,
    api_key: str,
    prompter: Callable[..., Any] | None = None,
) -> ReviewOutcome:
    """Send ``prompt`` to OpenAdapter and return the review text."""
    if not api_key.strip():
        message = "Set OPENADAPTOR_API_KEY to an OpenAdapter API key."
        print(message, file=sys.stderr)
        return ReviewOutcome(exit_code=EXIT_STARTUP, text=message)

    send = prompter or _agent_prompt
    try:
        result = send(prompt, api_key=api_key)
    except ReviewRequestError as err:
        message = f"startup failed: {err}"
        print(message, file=sys.stderr)
        return ReviewOutcome(exit_code=EXIT_STARTUP, text=message)

    run_id = getattr(result, "id", None)
    print(
        f"run_id={run_id} status={result.status} duration_ms={result.duration_ms}",
        file=sys.stderr,
    )

    if result.status != "finished":
        message = result.result or ""
        if message:
            print(message, file=sys.stderr)
        return ReviewOutcome(
            exit_code=EXIT_RUN_FAILED,
            text="The run did not finish.",
            run_id=str(run_id) if run_id else None,
            status=str(result.status),
        )

    return ReviewOutcome(
        exit_code=EXIT_OK,
        text=result.result or "",
        run_id=str(run_id) if run_id else None,
        status="finished",
    )


class ReviewRequestError(Exception):
    """The chat completion request failed before a review was returned."""


def _agent_prompt(prompt: str, *, api_key: str) -> Any:
    started = time.monotonic()
    payload = {
        "model": model_name(),
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You review GitHub pull requests. Follow the requested markdown "
                    "sections and do not edit code."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    request = urllib.request.Request(
        chat_completions_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")[:500]
        raise ReviewRequestError(f"OpenAdapter returned {err.code}: {detail}") from err
    except urllib.error.URLError as err:
        raise ReviewRequestError(f"Could not reach OpenAdapter: {err.reason}") from err

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ReviewRequestError("OpenAdapter returned a response that was not JSON.") from err
    text = _message_text(body)
    if not text:
        raise ReviewRequestError("OpenAdapter returned an empty review.")
    return _ChatResult(
        status="finished",
        result=text,
        run_id=str(body.get("id") or ""),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _message_text(body: dict) -> str:
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


class _ChatResult:
    def __init__(self, *, status: str, result: str, run_id: str, duration_ms: int) -> None:
        self.status = status
        self.result = result
        self.id = run_id or None
        self.duration_ms = duration_ms
