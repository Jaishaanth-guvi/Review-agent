import hashlib
import hmac
import json
import queue
import unittest
from contextlib import ExitStack
from unittest import mock

from pr_review.github_app import (
    COMMENT_MARKER,
    mint_installation_token,
    upsert_pull_request_comment,
    verify_signature,
)
from pr_review.github import PullRequest
from pr_review.review import ReviewOutcome
from pr_review.server import (
    ListenerConfig,
    ReviewBoard,
    ReviewJob,
    comment_from_outcome,
    config_from_env,
    handle_webhook,
    missing_review_credentials,
    process_review_job,
    review_job_from_event,
    status_page,
)


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _pull_payload(action: str, *, draft: bool = False) -> dict:
    return {
        "action": action,
        "installation": {"id": 99},
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 12, "draft": draft},
    }


class SignatureTests(unittest.TestCase):
    def test_accepts_matching_signature(self):
        body = b'{"zen":"ok"}'
        self.assertTrue(verify_signature(body, _sign(body, "topsecret"), "topsecret"))

    def test_rejects_tampered_body_or_secret(self):
        body = b'{"zen":"ok"}'
        signature = _sign(body, "topsecret")
        self.assertFalse(verify_signature(body + b" ", signature, "topsecret"))
        self.assertFalse(verify_signature(body, signature, "other"))
        self.assertFalse(verify_signature(body, None, "topsecret"))


class EventTests(unittest.TestCase):
    def test_opened_event_becomes_a_job(self):
        job = review_job_from_event("pull_request", _pull_payload("opened"))
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual((job.owner, job.repo, job.number, job.installation_id), ("acme", "widgets", 12, 99))

    def test_draft_is_skipped(self):
        for action in ("opened", "synchronize", "ready_for_review"):
            self.assertIsNone(
                review_job_from_event("pull_request", _pull_payload(action, draft=True))
            )

    def test_other_actions_are_skipped(self):
        self.assertIsNone(review_job_from_event("pull_request", _pull_payload("closed")))
        self.assertIsNone(review_job_from_event("ping", {"zen": "ok"}))


class CommentTests(unittest.TestCase):
    def test_creates_when_no_marker_comment_exists(self):
        calls = []

        def transport(method, url, token, payload=None):
            calls.append((method, url, token, payload))
            if method == "GET":
                return [{"id": 3, "body": "unrelated"}]
            return {"id": 8}

        upsert_pull_request_comment(
            "acme",
            "widgets",
            12,
            f"{COMMENT_MARKER}\nhello\n",
            "install-token",
            transport=transport,
        )
        self.assertEqual(calls[1][0], "POST")
        self.assertIn("/issues/12/comments", calls[1][1])
        self.assertEqual(calls[1][3]["body"], f"{COMMENT_MARKER}\nhello\n")

    def test_updates_existing_marker_comment(self):
        calls = []

        def transport(method, url, token, payload=None):
            calls.append((method, url, payload))
            if method == "GET":
                return [{"id": 7, "body": f"{COMMENT_MARKER}\nold"}]
            return {"id": 7}

        upsert_pull_request_comment(
            "acme",
            "widgets",
            12,
            f"{COMMENT_MARKER}\nnew\n",
            "install-token",
            transport=transport,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], "PATCH")
        self.assertIn("/issues/comments/7", calls[1][1])
        self.assertNotIn("/issues/12/comments", calls[1][1])

    def test_installation_token_uses_app_jwt(self):
        def transport(method, url, token, payload=None):
            self.assertEqual(method, "POST")
            self.assertIn("/app/installations/99/access_tokens", url)
            self.assertEqual(token, "app-jwt")
            return {"token": "ghs_install"}

        with mock.patch("pr_review.github_app.create_app_jwt", return_value="app-jwt"):
            token = mint_installation_token("123", "pem", 99, transport=transport)
        self.assertEqual(token, "ghs_install")


class WebhookTests(unittest.TestCase):
    def test_opened_event_is_queued_without_calling_the_agent(self):
        body = json.dumps(_pull_payload("opened")).encode()
        jobs = queue.Queue()
        with mock.patch("pr_review.server.run_review") as review:
            status, response = handle_webhook(
                body,
                {
                    "X-Hub-Signature-256": _sign(body, "secret"),
                    "X-GitHub-Event": "pull_request",
                },
                secret="secret",
                jobs=jobs,
            )
        review.assert_not_called()
        self.assertEqual(status, 200)
        self.assertEqual(response, b"ok")
        job = jobs.get_nowait()
        self.assertEqual(job.number, 12)
        self.assertTrue(jobs.empty())

    def test_draft_is_not_queued(self):
        body = json.dumps(_pull_payload("opened", draft=True)).encode()
        jobs = queue.Queue()
        status, _response = handle_webhook(
            body,
            {
                "X-Hub-Signature-256": _sign(body, "secret"),
                "X-GitHub-Event": "pull_request",
            },
            secret="secret",
            jobs=jobs,
        )
        self.assertEqual(status, 200)
        self.assertTrue(jobs.empty())

    def test_bad_signature_does_not_queue(self):
        body = json.dumps(_pull_payload("opened")).encode()
        jobs = queue.Queue()
        status, _response = handle_webhook(
            body,
            {
                "X-Hub-Signature-256": _sign(body, "other"),
                "X-GitHub-Event": "pull_request",
            },
            secret="secret",
            jobs=jobs,
        )
        self.assertEqual(status, 401)
        self.assertTrue(jobs.empty())


class ListenerConfigTests(unittest.TestCase):
    def test_webhook_secret_is_enough_to_listen(self):
        with mock.patch.dict(
            "os.environ",
            {"GITHUB_WEBHOOK_SECRET": "secret", "GITHUB_APP_PRIVATE_KEY": "", "GITHUB_APP_ID": "", "OPENADAPTOR_API_KEY": ""},
            clear=False,
        ):
            config = config_from_env(host="0.0.0.0", port=8000)
        self.assertEqual(config.webhook_secret, "secret")
        self.assertEqual(
            missing_review_credentials(config),
            ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "OPENADAPTOR_API_KEY"],
        )

    def test_missing_credentials_skip_the_review(self):
        job = ReviewJob("acme", "widgets", 12, 99, "opened")
        config = ListenerConfig(
            app_id="",
            private_key="",
            webhook_secret="secret",
            api_key="",
            host="127.0.0.1",
            port=8000,
        )
        with mock.patch("pr_review.server.mint_installation_token") as mint:
            process_review_job(job, config)
        mint.assert_not_called()


class ProcessJobTests(unittest.TestCase):
    def test_posts_the_review_from_the_diff(self):
        job = ReviewJob("acme", "widgets", 12, 99, "opened")
        config = ListenerConfig(
            app_id="1",
            private_key="pem",
            webhook_secret="secret",
            api_key="cursor",
            host="127.0.0.1",
            port=8000,
        )
        pull = PullRequest(
            owner="acme",
            repo="widgets",
            number=12,
            title="Add login",
            body="",
            url="https://github.com/acme/widgets/pull/12",
            author="ada",
            base_ref="main",
            head_ref="login",
            head_sha="abc",
            diff="diff --git a/a.py b/a.py\n",
        )
        posted = {}

        def upsert(owner, repo, number, body, token):
            posted["body"] = body
            posted["token"] = token
            posted["where"] = (owner, repo, number)

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch("pr_review.server.mint_installation_token", return_value="ghs_token")
            )
            stack.enter_context(mock.patch("pr_review.server.fetch_pull_request", return_value=pull))
            stack.enter_context(
                mock.patch(
                    "pr_review.server._dependency_context",
                    return_value="Dependents:\nNone.\n\nTests:\nNo test for the change was found.\n",
                )
            )
            review = stack.enter_context(
                mock.patch(
                    "pr_review.server.run_review",
                    return_value=ReviewOutcome(exit_code=0, text="### Summary\nFine.\n"),
                )
            )
            stack.enter_context(
                mock.patch("pr_review.server.upsert_pull_request_comment", side_effect=upsert)
            )
            process_review_job(job, config)

        self.assertEqual(review.call_args.kwargs["api_key"], "cursor")
        self.assertIn("diff --git", review.call_args.args[0])
        self.assertIn("No test for the change was found.", review.call_args.args[0])
        self.assertEqual(posted["where"], ("acme", "widgets", 12))
        self.assertEqual(posted["token"], "ghs_token")
        self.assertIn("### Summary", posted["body"])
        self.assertTrue(posted["body"].startswith(COMMENT_MARKER))


class ReviewBoardTests(unittest.TestCase):
    def test_snapshot_shows_a_running_review_and_the_queue(self):
        board = ReviewBoard(worker_count=1)
        running = ReviewJob("acme", "widgets", 12, 99, "opened")
        waiting = ReviewJob("acme", "widgets", 13, 99, "synchronize")
        board.queued(running)
        board.queued(waiting)
        board.started(1, running)
        snapshot = board.snapshot()
        self.assertTrue(snapshot["reviewing"])
        self.assertEqual(snapshot["active_reviews"], 1)
        self.assertEqual(snapshot["workers"][0]["state"], "reviewing")
        self.assertEqual(snapshot["workers"][0]["pull_request"], "acme/widgets#12")
        self.assertEqual(snapshot["queue"], [{"pull_request": "acme/widgets#13", "action": "synchronize"}])
        board.finished(1, running, "posted")
        done = board.snapshot()
        self.assertFalse(done["reviewing"])
        self.assertEqual(done["workers"][0]["state"], "idle")
        self.assertEqual(done["recent"][0]["result"], "posted")

    def test_status_page_names_workers(self):
        page = status_page()
        self.assertIn("Workers", page)
        self.assertIn("/status", page)


class CommentBodyTests(unittest.TestCase):
    def test_failure_names_the_run_and_has_no_verdict(self):
        body = comment_from_outcome(
            ReviewOutcome(
                exit_code=2,
                text="The run did not finish.",
                run_id="run-9",
                status="error",
            )
        )
        self.assertIn(COMMENT_MARKER, body)
        self.assertIn("no verdict", body)
        self.assertIn("run_id=run-9", body)
        self.assertNotIn("### Verdict", body)
        self.assertNotIn("approve", body.lower())

    def test_success_posts_the_review(self):
        body = comment_from_outcome(
            ReviewOutcome(exit_code=0, text="### Summary\nFine.\n\n### Verdict\napprove — ok\n")
        )
        self.assertTrue(body.startswith(COMMENT_MARKER))
        self.assertIn("### Summary", body)


if __name__ == "__main__":
    unittest.main()
