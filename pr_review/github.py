"""Download a pull request's metadata and diff."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

GITHUB_API = "https://api.github.com"
USER_AGENT = "pr-review-agent"


class GitHubError(Exception):
    """The pull request could not be downloaded."""


@dataclass(frozen=True)
class PullRequest:
    owner: str
    repo: str
    number: int
    title: str
    body: str
    url: str
    author: str
    base_ref: str
    head_ref: str
    head_sha: str
    diff: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


def fetch_pull_request(
    owner: str,
    repo: str,
    number: int,
    *,
    token: str | None = None,
    timeout: float = 30,
) -> PullRequest:
    """Download pull request metadata and the unified diff from the GitHub API."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}"
    try:
        payload = _request_json(url, token=token, timeout=timeout)
        diff = _request_text(
            url,
            token=token,
            timeout=timeout,
            accept="application/vnd.github.diff",
        )
    except urllib.error.HTTPError as err:
        detail = _http_error_detail(err)
        if err.code == 404:
            raise GitHubError(f"Pull request {owner}/{repo}#{number} was not found.") from err
        raise GitHubError(
            f"GitHub API returned {err.code} for {owner}/{repo}#{number}: {detail}"
        ) from err
    except urllib.error.URLError as err:
        raise GitHubError(f"Could not reach GitHub: {err.reason}") from err

    user = payload.get("user") or {}
    base = payload.get("base") or {}
    head = payload.get("head") or {}
    return PullRequest(
        owner=owner,
        repo=repo,
        number=number,
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
        url=str(payload.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}"),
        author=str(user.get("login") or "unknown"),
        base_ref=str(base.get("ref") or ""),
        head_ref=str(head.get("ref") or ""),
        head_sha=str(head.get("sha") or ""),
        diff=diff,
    )


def _request_json(url: str, *, token: str | None, timeout: float) -> dict:
    body = _request_text(
        url,
        token=token,
        timeout=timeout,
        accept="application/vnd.github+json",
    )
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise GitHubError(f"GitHub returned an unexpected payload for {url}.")
    return payload


def _request_text(url: str, *, token: str | None, timeout: float, accept: str) -> str:
    headers = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _http_error_detail(err: urllib.error.HTTPError) -> str:
    try:
        raw = err.read().decode("utf-8", errors="replace")
    except Exception:
        return err.reason if isinstance(err.reason, str) else str(err)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or str(err)
    message = payload.get("message")
    return str(message) if message else raw.strip()
