"""Build the review prompt sent to the model."""

from __future__ import annotations

import re

from pr_review.github import PullRequest

DEFAULT_MAX_DIFF_CHARS = 80_000
_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)

_INSTRUCTIONS = """\
You are reviewing a GitHub pull request. Report correctness bugs, security problems, broken behavior, a dependent file the change can break, and a risky change that has no test. Skip style nits and restating the diff.

The unified diff below is the change under review. Review that diff and the dependency note. Do not edit files, commit, or push.

Reply with nothing else.

If there is an issue, write one block per issue. Start with the file and line, then explain it in plain language so a reader can follow it without opening the code. Say what the person does, what goes wrong, and why. Add a short example when the behavior is not obvious from the line alone. Do not use section headings.

`path`:line — one sentence that names the problem

Then two to four sentences of explanation.

If there is no issue, write one short sentence about what the change fixes, then a new line:
ready for approve
"""


def changed_files(diff: str) -> list[str]:
    """Return file paths named by ``diff --git`` headers, in order."""
    files: list[str] = []
    for match in _DIFF_FILE.finditer(diff):
        path = match.group(2)
        if path not in files:
            files.append(path)
    return files


def build_review_prompt(
    pull_request: PullRequest,
    *,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
    context: str = "",
) -> str:
    """Assemble the one-shot review prompt, truncating an oversized diff."""
    if max_diff_chars < 1:
        raise ValueError("max_diff_chars must be positive")

    diff = pull_request.diff
    truncated = False
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars]
        truncated = True

    files = changed_files(pull_request.diff)
    file_list = "\n".join(f"- {path}" for path in files) if files else "- (no diff headers)"
    body = pull_request.body.strip() or "(no description)"
    truncation_note = ""
    if truncated:
        truncation_note = (
            f"\n\nThe diff was truncated to {max_diff_chars} characters. "
            "Files listed above that are missing from the truncated diff still changed.\n"
        )

    note = context.strip() or "Dependency check was unavailable."
    return (
        f"{_INSTRUCTIONS}\n"
        f"Pull request: {pull_request.slug}\n"
        f"URL: {pull_request.url}\n"
        f"Title: {pull_request.title}\n"
        f"Author: {pull_request.author}\n"
        f"Base: {pull_request.base_ref}\n"
        f"Head: {pull_request.head_ref} ({pull_request.head_sha})\n\n"
        f"Description:\n{body}\n\n"
        f"Files:\n{file_list}\n"
        f"{truncation_note}\n"
        f"{note}\n\n"
        f"Diff:\n```diff\n{diff}\n```\n"
    )
