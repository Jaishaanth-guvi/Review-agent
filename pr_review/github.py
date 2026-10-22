"""Resolve a pull request reference and download its metadata and diff."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

GITHUB_API = "https://api.github.com"
USER_AGENT = "pr-review-agent"

_PR_URL = re.compile(
    r"^https?://github\.com/"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)(?:/.*)?$"
)
_PR_SHORT = re.compile(
    r"^(?P<owner>[^/]+)/(?P<repo>[^/#]+)(?:#(?P<number>\d+)|/pull/(?P<number2>\d+))$"
)
_PR_NUMBER = re.compile(r"^(?P<number>\d+)$")
_REMOTE = re.compile(
    r"github\.com[:/](?P<owner>[^/:]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)
_REPO_SLUG = re.compile(r"^(?P<owner>[^/]+)/(?P<repo>[^/]+)$")


class GitHubError(Exception):
    """The pull request could not be resolved or downloaded."""


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


def parse_repo_slug(value: str) -> tuple[str, str]:
    """Parse ``owner/repo`` into its two parts."""
    match = _REPO_SLUG.fullmatch(value.strip())
    if match is None:
        raise GitHubError(f"Expected owner/repo, got {value!r}.")
    return match.group("owner"), _strip_git_suffix(match.group("repo"))


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` when ``url`` points at a GitHub repository."""
    match = _REMOTE.search(url.strip())
    if match is None:
        return None
    return match.group("owner"), _strip_git_suffix(match.group("repo"))


def resolve_pull_request_ref(
    value: str,
    *,
    repo: str | None = None,
    cwd: str = ".",
    remote_url: str | None = None,
    github_repository: str | None = None,
) -> tuple[str, str, int]:
    """Turn a URL, ``owner/repo#n``, or a bare number into owner, repo, and number.

    A bare number uses ``repo`` when given, then ``github_repository``, then
    ``remote_url``, then ``origin`` in ``cwd``.
    """
    text = value.strip()
    url_match = _PR_URL.fullmatch(text)
    if url_match is not None:
        owner, name = url_match.group("owner"), _strip_git_suffix(url_match.group("repo"))
        _reject_repo_mismatch(repo, owner, name)
        return owner, name, int(url_match.group("number"))

    short_match = _PR_SHORT.fullmatch(text)
    if short_match is not None:
        number = short_match.group("number") or short_match.group("number2")
        owner, name = short_match.group("owner"), _strip_git_suffix(short_match.group("repo"))
        _reject_repo_mismatch(repo, owner, name)
        return owner, name, int(number)

    number_match = _PR_NUMBER.fullmatch(text)
    if number_match is None:
        raise GitHubError(
            "Pass a GitHub pull request URL, owner/repo#123, or a pull request number."
        )

    owner, name = _repo_for_number(
        repo=repo,
        cwd=cwd,
        remote_url=remote_url,
        github_repository=github_repository,
    )
    return owner, name, int(number_match.group("number"))


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
            raise GitHubError(
                f"Pull request {owner}/{repo}#{number} was not found. "
                "For a private repository, set GITHUB_TOKEN or GH_TOKEN."
            ) from err
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


def origin_remote_url(cwd: str) -> str | None:
    """Return the ``origin`` remote URL for ``cwd``, or ``None`` when git has none."""
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    url = completed.stdout.strip()
    return url or None


def github_token_from_env() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    return token.strip() if token else None


def _repo_for_number(
    *,
    repo: str | None,
    cwd: str,
    remote_url: str | None,
    github_repository: str | None,
) -> tuple[str, str]:
    if repo:
        return parse_repo_slug(repo)
    if github_repository:
        return parse_repo_slug(github_repository)
    url = remote_url if remote_url is not None else origin_remote_url(cwd)
    if url:
        parsed = parse_github_remote(url)
        if parsed is not None:
            return parsed
    raise GitHubError(
        "A bare pull request number needs a repository. "
        "Pass --repo owner/repo, set GITHUB_REPOSITORY, or run inside a checkout whose origin is GitHub."
    )


def _reject_repo_mismatch(repo: str | None, owner: str, name: str) -> None:
    if not repo:
        return
    flag_owner, flag_name = parse_repo_slug(repo)
    if (flag_owner, flag_name) != (owner, name):
        raise GitHubError(
            f"--repo {flag_owner}/{flag_name} does not match {owner}/{name} in the pull request reference."
        )


def _strip_git_suffix(name: str) -> str:
    return name[:-4] if name.endswith(".git") else name


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
