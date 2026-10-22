import io
import unittest
from unittest import mock

from pr_review.review import EXIT_OK, EXIT_RUN_FAILED, EXIT_STARTUP, run_review


class Result:
    def __init__(self, status, result="", duration_ms=15, run_id="run-1"):
        self.status = status
        self.result = result
        self.duration_ms = duration_ms
        self.id = run_id
        self.agent_id = "agent-1"


class RunReviewTests(unittest.TestCase):
    def test_missing_api_key_does_not_call_agent(self):
        called = False

        def prompter(*_args, **_kwargs):
            nonlocal called
            called = True

        with mock.patch("sys.stderr", new_callable=io.StringIO):
            code = run_review("review", cwd=".", model="composer-2.5", api_key="  ", prompter=prompter)
        self.assertEqual(code, EXIT_STARTUP)
        self.assertFalse(called)

    def test_finished_review_goes_to_stdout(self):
        def prompter(prompt, *, cwd, model, api_key):
            self.assertEqual(cwd, "/repo")
            self.assertEqual(model, "composer-2.5")
            self.assertEqual(api_key, "key")
            self.assertIn("review this", prompt)
            return Result("finished", "### Summary\nLooks fine.\n")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = run_review(
                "review this",
                cwd="/repo",
                model="composer-2.5",
                api_key="key",
                prompter=prompter,
            )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("### Summary", stdout.getvalue())
        self.assertIn("agent_id=agent-1", stderr.getvalue())
        self.assertIn("run_id=run-1", stderr.getvalue())

    def test_run_error_exits_2(self):
        def prompter(*_args, **_kwargs):
            return Result("error", "blew up")

        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = run_review("review", cwd=".", model="composer-2.5", api_key="key", prompter=prompter)
        self.assertEqual(code, EXIT_RUN_FAILED)
        self.assertIn("blew up", stderr.getvalue())

    def test_cursor_agent_error_exits_1(self):
        class CursorAgentError(Exception):
            def __init__(self):
                super().__init__("invalid key")
                self.is_retryable = False
                self.request_id = "req-9"

        def prompter(*_args, **_kwargs):
            raise CursorAgentError()

        with mock.patch.dict("sys.modules", {"cursor_sdk": mock.Mock(CursorAgentError=CursorAgentError)}):
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                code = run_review(
                    "review",
                    cwd=".",
                    model="composer-2.5",
                    api_key="key",
                    prompter=prompter,
                )
        self.assertEqual(code, EXIT_STARTUP)
        self.assertIn("request_id=req-9", stderr.getvalue())
        self.assertIn("retryable=False", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
