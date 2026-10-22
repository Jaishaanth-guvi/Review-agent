"""Build the review prompt sent to the model."""

from __future__ import annotations

import re

from pr_review.github import PullRequest

DEFAULT_MAX_DIFF_CHARS = 80_000
_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)

_INSTRUCTIONS = """\
You are reviewing a GitHub pull request. Report issues a reviewer should act on: correctness bugs, security problems, broken behavior, and missing tests for risky changes. Skip style nits and restating the diff.

The unified diff below is the change under review. Review that diff. Do not edit files, commit, or push.

Reply in markdown with only these sections:

### Summary
2-4 sentences on what changed and whether the change holds together.

### Findings
One bullet per finding:
- **critical** or **major** or **minor**: `path` — what is wrong, why it matters, and a one-sentence fix.

If there is nothing to report, write `None.`

### Verdict
One line: `approve` or `request changes`, then a brief reason.
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
        f"Diff:\n```diff\n{diff}\n```\n"
    )
