"""Command-line entry point for the pull request review agent."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pr_review import __version__
from pr_review.github import (
    GitHubError,
    fetch_pull_request,
    github_token_from_env,
    resolve_pull_request_ref,
)
from pr_review.prompt import DEFAULT_MAX_DIFF_CHARS, build_review_prompt
from pr_review.review import run_review

EXIT_USAGE = 3


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    cwd = str(Path(args.cwd).expanduser().resolve())
    if not Path(cwd).is_dir():
        print(f"Workspace does not exist: {cwd}", file=sys.stderr)
        return EXIT_USAGE

    try:
        owner, repo, number = resolve_pull_request_ref(
            args.pr,
            repo=args.repo,
            cwd=cwd,
            github_repository=os.environ.get("GITHUB_REPOSITORY"),
        )
        pull_request = fetch_pull_request(
            owner,
            repo,
            number,
            token=github_token_from_env(),
        )
    except GitHubError as err:
        print(str(err), file=sys.stderr)
        return EXIT_USAGE

    prompt = build_review_prompt(pull_request, max_diff_chars=args.max_diff_chars)
    if args.dry_run:
        sys.stdout.write(prompt)
        if not prompt.endswith("\n"):
            sys.stdout.write("\n")
        return 0

    api_key = args.api_key if args.api_key is not None else os.environ.get("CURSOR_API_KEY", "")
    print(f"Reviewing {pull_request.slug} in {cwd}", file=sys.stderr)
    return run_review(
        prompt,
        cwd=cwd,
        model=args.model,
        api_key=api_key,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-review",
        description=(
            "Review a GitHub pull request with a local Cursor agent and print the findings."
        ),
    )
    parser.add_argument(
        "pr",
        help="Pull request URL, owner/repo#123, or a number when the repo can be inferred.",
    )
    parser.add_argument(
        "--repo",
        help="GitHub repository as owner/repo. Used when pr is a bare number.",
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Local checkout the agent may read for surrounding code. Defaults to the current directory.",
    )
    parser.add_argument(
        "--model",
        default="composer-2.5",
        help="Cursor model id. Defaults to composer-2.5.",
    )
    parser.add_argument(
        "--api-key",
        help="Cursor API key. Defaults to CURSOR_API_KEY.",
    )
    parser.add_argument(
        "--max-diff-chars",
        type=int,
        default=DEFAULT_MAX_DIFF_CHARS,
        help=f"Maximum diff characters included in the prompt. Defaults to {DEFAULT_MAX_DIFF_CHARS}.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompt and skip the agent.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser
