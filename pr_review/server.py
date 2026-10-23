"""Listen for GitHub App pull request events and post a review comment."""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pr_review.dependencies import dependency_note
from pr_review.github import GitHubError, fetch_pull_request
from pr_review.github_app import (
    COMMENT_MARKER,
    load_private_key,
    mint_installation_token,
    upsert_pull_request_comment,
    verify_signature,
)
from pr_review.prompt import build_review_prompt, changed_files
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

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


class ReviewBoard:
    """In-memory view of workers, the waiting queue, and recent reviews."""

    def __init__(self, worker_count: int = 1) -> None:
        self._lock = threading.Lock()
        self._waiting: list[dict[str, str]] = []
        self._workers: dict[int, dict[str, str | None]] = {
            worker_id: {"id": str(worker_id), "state": "idle", "pull_request": None, "action": None}
            for worker_id in range(1, worker_count + 1)
        }
        self._recent: list[dict[str, str]] = []

    def queued(self, job: ReviewJob) -> None:
        with self._lock:
            self._waiting.append({"pull_request": job.label, "action": job.action})

    def started(self, worker_id: int, job: ReviewJob) -> None:
        with self._lock:
            for index, item in enumerate(self._waiting):
                if item["pull_request"] == job.label and item["action"] == job.action:
                    del self._waiting[index]
                    break
            self._workers[worker_id] = {
                "id": str(worker_id),
                "state": "reviewing",
                "pull_request": job.label,
                "action": job.action,
            }

    def finished(self, worker_id: int, job: ReviewJob, result: str) -> None:
        with self._lock:
            self._workers[worker_id] = {
                "id": str(worker_id),
                "state": "idle",
                "pull_request": None,
                "action": None,
            }
            self._recent.insert(
                0,
                {
                    "pull_request": job.label,
                    "action": job.action,
                    "result": result,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            del self._recent[12:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            workers = [dict(worker) for worker in self._workers.values()]
            waiting = [dict(item) for item in self._waiting]
            recent = [dict(item) for item in self._recent]
        active = sum(1 for worker in workers if worker["state"] == "reviewing")
        return {
            "reviewing": active > 0,
            "active_reviews": active,
            "waiting": len(waiting),
            "workers": workers,
            "queue": waiting,
            "recent": recent,
        }


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
    board: ReviewBoard | None = None,
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
    if board is not None:
        board.queued(job)
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


def process_review_job(job: ReviewJob, config: ListenerConfig) -> str:
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
        return "skipped"
    token = mint_installation_token(config.app_id, config.private_key, job.installation_id)
    try:
        pull_request = fetch_pull_request(job.owner, job.repo, job.number, token=token)
        context = _dependency_context(job, token, pull_request.diff)
        prompt = build_review_prompt(pull_request, context=context)
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
    if outcome.exit_code == EXIT_OK and outcome.text.strip():
        return "posted"
    return "failed"


def _dependency_context(job: ReviewJob, token: str, diff: str) -> str:
    """Scan the pull request head. A scan failure does not stop the review."""
    try:
        with tempfile.TemporaryDirectory(prefix="pr-review-") as checkout:
            clone_pull_request_head(job.owner, job.repo, job.number, token, checkout)
            return dependency_note(checkout, changed_files(diff))
    except Exception as err:
        print(
            f"dependency check failed for {job.owner}/{job.repo}#{job.number}: {err}",
            file=sys.stderr,
        )
        return "Dependency check was unavailable."


def git_authorization_header(token: str) -> str:
    """Basic auth header Git accepts for a GitHub installation token."""
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return f"AUTHORIZATION: basic {encoded}"


def clone_pull_request_head(
    owner: str,
    repo: str,
    number: int,
    token: str,
    dest: str,
) -> None:
    """Shallow-fetch ``refs/pull/<number>/head`` into ``dest``."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraheader"
    env["GIT_CONFIG_VALUE_0"] = git_authorization_header(token)
    remote = f"https://github.com/{owner}/{repo}.git"
    ref = f"refs/pull/{number}/head"
    _git(["init", dest], env)
    _git(["-C", dest, "fetch", "--depth", "1", remote, ref], env)
    _git(["-C", dest, "checkout", "--detach", "FETCH_HEAD"], env)


def _git(args: list[str], env: dict[str, str]) -> None:
    completed = subprocess.run(
        ["git", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "git failed").strip()
    last_line = detail.splitlines()[-1] if detail else "git failed"
    raise GitHubError(last_line[:500])


def worker_loop(
    jobs: queue.Queue[ReviewJob],
    config: ListenerConfig,
    board: ReviewBoard,
    worker_id: int,
) -> None:
    """Review queued pull requests until the process exits."""
    while True:
        job = jobs.get()
        board.started(worker_id, job)
        result = "failed"
        try:
            result = process_review_job(job, config)
        except Exception as err:
            print(
                f"review failed for {job.owner}/{job.repo}#{job.number}: {err}",
                file=sys.stderr,
            )
        finally:
            board.finished(worker_id, job, result)
            jobs.task_done()


def serve(config: ListenerConfig, jobs: queue.Queue[ReviewJob], board: ReviewBoard) -> None:
    """Serve webhook deliveries and the status page until interrupted."""
    handler = _handler_class(config.webhook_secret, jobs, board)
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
    board = ReviewBoard(worker_count=1)
    worker = threading.Thread(
        target=worker_loop,
        args=(jobs, config, board, 1),
        name="pr-review-worker",
        daemon=True,
    )
    worker.start()
    serve(config, jobs, board)
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


def status_page() -> str:
    """Return the worker status page. The page reads `/status` in the browser."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Review agent</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f3f0e8;
      --ink: #1c1915;
      --muted: #6b645b;
      --card: #fffdf8;
      --line: #e4ddd2;
      --accent: #0f6e56;
      --idle: #c8c0b4;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #141210;
        --ink: #f6f1e8;
        --muted: #b7aea2;
        --card: #221e19;
        --line: #3a332b;
        --accent: #3dbe9a;
        --idle: #5c554c;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font: 16px/1.45 ui-sans-serif, system-ui, sans-serif;
      background:
        radial-gradient(circle at top left, color-mix(in srgb, var(--accent) 18%, transparent), transparent 32%),
        var(--bg);
      color: var(--ink);
    }
    main { width: min(840px, calc(100% - 32px)); margin: 32px auto 48px; }
    h1 { margin: 8px 0 0; font-size: 2rem; }
    h2 { margin: 0 0 12px; font-size: 0.95rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); }
    .board { display: grid; grid-template-columns: 180px 1fr; gap: 18px; align-items: center; }
    .meter { width: 160px; height: 160px; }
    .meter circle { fill: none; stroke-width: 14; }
    .track { stroke: var(--line); }
    .value { stroke: var(--idle); stroke-linecap: round; transform: rotate(-90deg); transform-origin: 80px 80px; transition: stroke-dashoffset 0.4s ease, stroke 0.4s ease; }
    .meter.busy .value { stroke: var(--accent); }
    .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    .stat, .lane, .chip, .event { background: var(--card); border: 1px solid var(--line); border-radius: 16px; }
    .stat { padding: 14px 16px; }
    .stat strong { display: block; font-size: 1.8rem; }
    .stat span, .muted { color: var(--muted); }
    section { margin-top: 26px; }
    .lane { display: grid; grid-template-columns: 16px 1fr; gap: 12px; align-items: center; padding: 14px 16px; margin: 0 0 10px; }
    .dot { width: 16px; height: 16px; border-radius: 50%; background: var(--idle); }
    .lane.busy .dot { background: var(--accent); box-shadow: 0 0 0 6px color-mix(in srgb, var(--accent) 25%, transparent); }
    .lane h3 { margin: 0; font-size: 1rem; }
    .lane p { margin: 2px 0 0; }
    .bar { height: 8px; margin-top: 10px; border-radius: 99px; background: var(--line); overflow: hidden; }
    .bar i { display: block; height: 100%; width: 0; background: var(--accent); }
    .lane.busy .bar i { width: 100%; animation: slide 1.4s ease-in-out infinite; }
    @keyframes slide { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
    .chips, .timeline { display: grid; gap: 8px; }
    .chip, .event { padding: 12px 14px; }
    @media (max-width: 640px) {
      .board, .stats { grid-template-columns: 1fr; }
      .meter { margin: 0 auto; }
    }
  </style>
</head>
<body>
  <main>
    <div class="board">
      <svg class="meter" id="meter" viewBox="0 0 160 160" role="img" aria-label="Worker activity">
        <circle class="track" cx="80" cy="80" r="62"></circle>
        <circle class="value" id="arc" cx="80" cy="80" r="62"></circle>
      </svg>
      <div>
        <h1 id="headline">Checking workers</h1>
        <p class="muted" id="summary"></p>
        <div class="stats" aria-label="Counts">
          <article class="stat"><strong id="working">0</strong><span>Working</span></article>
          <article class="stat"><strong id="waiting">0</strong><span>Waiting</span></article>
          <article class="stat"><strong id="finished">0</strong><span>Finished</span></article>
        </div>
      </div>
    </div>
    <section>
      <h2>Workers</h2>
      <div id="workers"></div>
    </section>
    <section>
      <h2>Waiting</h2>
      <div class="chips" id="queue"></div>
    </section>
    <section>
      <h2>Recent</h2>
      <div class="timeline" id="recent"></div>
    </section>
  </main>
  <script>
    const arc = document.getElementById("arc");
    const radius = 62;
    const length = 2 * Math.PI * radius;
    arc.style.strokeDasharray = String(length);

    function textItem(className, text) {
      const item = document.createElement("p");
      item.className = className;
      item.textContent = text;
      return item;
    }
    function render(data) {
      const total = Math.max(data.workers.length, 1);
      const busy = Boolean(data.reviewing);
      const filled = length * (1 - (data.active_reviews / total));
      arc.style.strokeDashoffset = String(filled);
      arc.setAttribute("stroke", busy ? "#1f9d78" : "#8a8175");
      document.getElementById("meter").className = busy ? "meter busy" : "meter";
      document.getElementById("headline").textContent = busy ? "A review is happening" : "No review is happening";
      document.getElementById("summary").textContent = data.active_reviews + " of " + total + " workers are reviewing";
      document.getElementById("working").textContent = String(data.active_reviews);
      document.getElementById("waiting").textContent = String(data.waiting);
      document.getElementById("finished").textContent = String(data.recent.length);

      const workers = document.getElementById("workers");
      workers.replaceChildren();
      for (const worker of data.workers) {
        const card = document.createElement("article");
        const reviewing = worker.state === "reviewing";
        card.className = reviewing ? "lane busy" : "lane";
        const dot = document.createElement("span");
        dot.className = "dot";
        dot.setAttribute("aria-hidden", "true");
        const copy = document.createElement("div");
        const title = document.createElement("h3");
        title.textContent = "Worker " + worker.id;
        copy.append(title);
        copy.append(textItem("", reviewing
          ? "Reviewing " + worker.pull_request + " (" + worker.action + ")"
          : "Idle"));
        const bar = document.createElement("div");
        bar.className = "bar";
        bar.append(document.createElement("i"));
        copy.append(bar);
        card.append(dot, copy);
        workers.append(card);
      }

      const queue = document.getElementById("queue");
      queue.replaceChildren();
      if (!data.queue.length) queue.append(textItem("muted", "Nothing is waiting."));
      for (const job of data.queue) {
        const item = document.createElement("article");
        item.className = "chip";
        item.textContent = job.pull_request + " (" + job.action + ")";
        queue.append(item);
      }

      const recent = document.getElementById("recent");
      recent.replaceChildren();
      if (!data.recent.length) recent.append(textItem("muted", "No reviews yet."));
      for (const job of data.recent) {
        const item = document.createElement("article");
        item.className = "event";
        item.textContent = job.pull_request + " — " + job.result + " (" + job.action + ")";
        recent.append(item);
      }
    }
    async function load() {
      const response = await fetch("/status", { cache: "no-store" });
      render(await response.json());
    }
    load();
    setInterval(load, 2000);
  </script>
</body>
</html>
"""


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.lower()
    for key, value in headers.items():
        if key.lower() == folded:
            return value
    return None


def _handler_class(
    secret: str,
    jobs: queue.Queue[ReviewJob],
    board: ReviewBoard,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/status":
                body = json.dumps(board.snapshot()).encode("utf-8")
                self._reply(200, body, "application/json; charset=utf-8")
                return
            if path in {"/", "/ui"}:
                self._reply(200, status_page().encode("utf-8"), "text/html; charset=utf-8")
                return
            self._reply(404, b"not found")

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
                board=board,
            )
            self._reply(status, response)

        def _reply(self, status: int, response: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"[webhook] {self.address_string()} {fmt % args}", file=sys.stderr)

    return Handler
