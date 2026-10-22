"""Listen for GitHub App pull request events and post a review comment."""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pr_review.github import GitHubError, fetch_pull_request
from pr_review.github_app import (
    COMMENT_MARKER,
    load_private_key,
    mint_installation_token,
    upsert_pull_request_comment,
    verify_signature,
)
from pr_review.prompt import build_review_prompt
from pr_review.review import EXIT_OK, ReviewOutcome, run_review

REVIEW_ACTIONS = frozenset({"opened", "synchronize", "ready_for_review"})
MAX_BODY_BYTES = 2_000_000


@dataclass(frozen=True)
class ListenerConfig:
    app_id: str
    private_key: str
    webhook_secret: str
    api_key: str
    host: str
    port: int


@dataclass(frozen=True)
class ReviewJob:
    owner: str
    repo: str
    number: int
    installation_id: int
    action: str


def config_from_env(*, host: str, port: int) -> ListenerConfig:
    """Read listener settings from the environment.

    The webhook secret is required to accept deliveries. Review credentials can
    be added later; until they are set the process still listens.
    """
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()
    api_key = os.environ.get("OPENADAPTOR_API_KEY", "").strip()
    private_key_value = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
    if not webhook_secret:
        raise GitHubError("Missing environment variable: GITHUB_WEBHOOK_SECRET.")
    private_key = load_private_key(private_key_value) if private_key_value.strip() else ""
    return ListenerConfig(
        app_id=app_id,
        private_key=private_key,
        webhook_secret=webhook_secret,
        api_key=api_key,
        host=host,
        port=port,
    )


def missing_review_credentials(config: ListenerConfig) -> list[str]:
    """Return the environment variables still needed to review a pull request."""
    missing = []
    if not config.app_id:
        missing.append("GITHUB_APP_ID")
    if not config.private_key:
        missing.append("GITHUB_APP_PRIVATE_KEY")
    if not config.api_key:
        missing.append("OPENADAPTOR_API_KEY")
    return missing


def review_job_from_event(event_name: str, payload: Mapping[str, Any]) -> ReviewJob | None:
    """Return a review job, or ``None`` when this event should be ignored.

    Draft pull requests are ignored. A malformed reviewable event raises
    ``GitHubError``.
    """
    if event_name != "pull_request":
        return None
    action = str(payload.get("action") or "")
    if action not in REVIEW_ACTIONS:
        return None
    pull_request = payload.get("pull_request") or {}
    if not isinstance(pull_request, Mapping):
        raise GitHubError("Pull request event is missing the pull_request object.")
    if pull_request.get("draft") is True:
        return None

    number = pull_request.get("number")
    repository = payload.get("repository") or {}
    full_name = str(repository.get("full_name") or "") if isinstance(repository, Mapping) else ""
    installation = payload.get("installation") or {}
    installation_id = installation.get("id") if isinstance(installation, Mapping) else None
    if not number or "/" not in full_name or not installation_id:
        raise GitHubError(
            "Pull request event is missing repository, number, or installation."
        )
    owner, repo = full_name.split("/", 1)
    return ReviewJob(
        owner=owner,
        repo=repo,
        number=int(number),
        installation_id=int(installation_id),
        action=action,
    )


def handle_webhook(
    body: bytes,
    headers: Mapping[str, str],
    *,
    secret: str,
    jobs: queue.Queue[ReviewJob],
) -> tuple[int, bytes]:
    """Verify the delivery and queue a review. Does not run the agent."""
    if len(body) > MAX_BODY_BYTES:
        return 413, b"payload too large"
    signature = _header(headers, "X-Hub-Signature-256")
    if not verify_signature(body, signature, secret):
        return 401, b"invalid signature"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, b"invalid payload"
    if not isinstance(payload, dict):
        return 400, b"invalid payload"

    event_name = _header(headers, "X-GitHub-Event") or ""
    try:
        job = review_job_from_event(event_name, payload)
    except GitHubError as err:
        print(str(err), file=sys.stderr)
        return 200, b"ignored"
    if job is None:
        return 200, b"ignored"
    jobs.put(job)
    print(
        f"Queued {job.owner}/{job.repo}#{job.number} ({job.action})",
        file=sys.stderr,
    )
    return 200, b"ok"


def comment_from_outcome(outcome: ReviewOutcome) -> str:
    """Turn a review outcome into the pull request comment body."""
    if outcome.exit_code == EXIT_OK and outcome.text.strip():
        return f"{COMMENT_MARKER}\n{outcome.text.strip()}\n"
    if outcome.exit_code == EXIT_OK:
        detail = "The review finished without any text."
    elif outcome.status is None:
        detail = outcome.text.strip() or "The review did not finish."
    else:
        detail = "The run did not finish."
    return _failure_comment(outcome, detail)


def process_review_job(job: ReviewJob, config: ListenerConfig) -> None:
    """Fetch the pull request diff, review it, and post or update the comment."""
    print(
        f"Reviewing {job.owner}/{job.repo}#{job.number}",
        file=sys.stderr,
    )
    missing = missing_review_credentials(config)
    if missing:
        print(
            f"Cannot review {job.owner}/{job.repo}#{job.number}. "
            "Set " + ", ".join(missing) + ".",
            file=sys.stderr,
        )
        return
    token = mint_installation_token(config.app_id, config.private_key, job.installation_id)
    try:
        pull_request = fetch_pull_request(job.owner, job.repo, job.number, token=token)
        prompt = build_review_prompt(pull_request)
        outcome = run_review(prompt, api_key=config.api_key)
    except Exception as err:
        print(
            f"review failed for {job.owner}/{job.repo}#{job.number}: {err}",
            file=sys.stderr,
        )
        outcome = ReviewOutcome(exit_code=1, text="The review could not be prepared.")
    upsert_pull_request_comment(
        job.owner,
        job.repo,
        job.number,
        comment_from_outcome(outcome),
        token,
    )


def worker_loop(jobs: queue.Queue[ReviewJob], config: ListenerConfig) -> None:
    """Review queued pull requests until the process exits."""
    while True:
        job = jobs.get()
        try:
            process_review_job(job, config)
        except Exception as err:
            print(
                f"review failed for {job.owner}/{job.repo}#{job.number}: {err}",
                file=sys.stderr,
            )
        finally:
            jobs.task_done()


def serve(config: ListenerConfig, jobs: queue.Queue[ReviewJob]) -> None:
    """Serve webhook deliveries until interrupted."""
    handler = _handler_class(config.webhook_secret, jobs)
    server = ThreadingHTTPServer((config.host, config.port), handler)
    print(f"Listening on {config.host}:{config.port}", file=sys.stderr)
    missing = missing_review_credentials(config)
    if missing:
        print(
            "Webhook is listening. Reviews start after you set " + ", ".join(missing) + ".",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.", file=sys.stderr)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pr-review-listen",
        description=(
            "Listen for GitHub App pull request events, review each one, "
            "and post the findings as a comment."
        ),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Address to bind. Defaults to HOST or 0.0.0.0.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Port to bind. Defaults to PORT or 8000.",
    )
    args = parser.parse_args(argv)
    try:
        config = config_from_env(host=args.host, port=args.port)
    except GitHubError as err:
        print(str(err), file=sys.stderr)
        return 1

    jobs: queue.Queue[ReviewJob] = queue.Queue()
    worker = threading.Thread(
        target=worker_loop,
        args=(jobs, config),
        name="pr-review-worker",
        daemon=True,
    )
    worker.start()
    serve(config, jobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())


def _failure_comment(outcome: ReviewOutcome, detail: str) -> str:
    lines = [
        COMMENT_MARKER,
        "The review did not finish, so there is no verdict.",
        "",
        detail,
    ]
    if outcome.status:
        lines.append(f"status={outcome.status}")
    if outcome.run_id:
        lines.append(f"run_id={outcome.run_id}")
    return "\n".join(lines) + "\n"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.lower()
    for key, value in headers.items():
        if key.lower() == folded:
            return value
    return None


def _handler_class(secret: str, jobs: queue.Queue[ReviewJob]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length_header = self.headers.get("Content-Length", "0")
            try:
                length = int(length_header)
            except ValueError:
                self._reply(400, b"invalid payload")
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._reply(413, b"payload too large")
                return
            body = self.rfile.read(length)
            status, response = handle_webhook(
                body,
                {key: value for key, value in self.headers.items()},
                secret=secret,
                jobs=jobs,
            )
            self._reply(status, response)

        def _reply(self, status: int, response: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"[webhook] {self.address_string()} {fmt % args}", file=sys.stderr)

    return Handler
