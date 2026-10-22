"""GitHub App authentication, webhook signatures, and pull request comments."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from pr_review.github import GITHUB_API, USER_AGENT, GitHubError

COMMENT_MARKER = "<!-- pr-review-agent -->"
MAX_COMMENT_CHARS = 65_000

Transport = Callable[[str, str, str, Optional[dict]], Any]


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Return whether ``signature_header`` is the HMAC-SHA256 of ``body``."""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    if len(expected) != len(signature_header):
        return False
    return hmac.compare_digest(expected, signature_header)


def load_private_key(value: str) -> str:
    """Load a PEM private key from a file path or from the string itself."""
    text = value.strip()
    if not text:
        raise GitHubError("GITHUB_APP_PRIVATE_KEY is empty.")
    if "BEGIN" not in text:
        path = Path(text).expanduser()
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
    else:
        text = text.replace("\\n", "\n")
    if "BEGIN" not in text:
        raise GitHubError(
            "GITHUB_APP_PRIVATE_KEY must be a PEM private key or a path to one."
        )
    return text


def create_app_jwt(app_id: str, private_key: str) -> str:
    """Sign a short-lived GitHub App JWT."""
    import jwt

    now = int(time.time())
    token = jwt.encode(
        {"iat": now - 60, "exp": now + 9 * 60, "iss": str(app_id)},
        private_key,
        algorithm="RS256",
    )
    if isinstance(token, bytes):
        return token.decode("utf-8")
    return token


def mint_installation_token(
    app_id: str,
    private_key: str,
    installation_id: int,
    *,
    transport: Transport | None = None,
) -> str:
    """Exchange an app JWT for an installation access token."""
    app_jwt = create_app_jwt(app_id, private_key)
    send = transport or github_request
    payload = send(
        "POST",
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        app_jwt,
        {},
    )
    if not isinstance(payload, dict) or not payload.get("token"):
        raise GitHubError("GitHub did not return an installation token.")
    return str(payload["token"])


def upsert_pull_request_comment(
    owner: str,
    repo: str,
    number: int,
    body: str,
    token: str,
    *,
    transport: Transport | None = None,
) -> None:
    """Create the review comment, or update the existing one that carries the marker."""
    if len(body) > MAX_COMMENT_CHARS:
        body = body[: MAX_COMMENT_CHARS - 20].rstrip() + "\n\n(truncated)\n"
    send = transport or github_request
    existing_id = _find_comment_id(owner, repo, number, token, send)
    if existing_id is None:
        send(
            "POST",
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments",
            token,
            {"body": body},
        )
        return
    send(
        "PATCH",
        f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{existing_id}",
        token,
        {"body": body},
    )


def github_request(method: str, url: str, token: str, payload: dict | None = None) -> Any:
    """Send one GitHub API request and return the decoded JSON body."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        detail = _http_error_detail(err)
        raise GitHubError(f"GitHub API returned {err.code} for {method} {url}: {detail}") from err
    except urllib.error.URLError as err:
        raise GitHubError(f"Could not reach GitHub: {err.reason}") from err
    if not raw.strip():
        return {}
    decoded = json.loads(raw)
    return decoded


def _find_comment_id(
    owner: str,
    repo: str,
    number: int,
    token: str,
    send: Transport,
) -> int | None:
    page = 1
    while page <= 10:
        comments = send(
            "GET",
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments"
            f"?per_page=100&page={page}",
            token,
            None,
        )
        if not isinstance(comments, list) or not comments:
            return None
        for comment in comments:
            if isinstance(comment, dict) and COMMENT_MARKER in str(comment.get("body") or ""):
                return int(comment["id"])
        if len(comments) < 100:
            return None
        page += 1
    return None


def _http_error_detail(err: urllib.error.HTTPError) -> str:
    try:
        raw = err.read().decode("utf-8", errors="replace")
    except Exception:
        return err.reason if isinstance(err.reason, str) else str(err)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or str(err)
    message = payload.get("message") if isinstance(payload, dict) else None
    return str(message) if message else raw.strip()
