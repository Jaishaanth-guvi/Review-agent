import io
import json
import unittest
from unittest import mock

from pr_review.review import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    EXIT_OK,
    EXIT_RUN_FAILED,
    EXIT_STARTUP,
    USER_AGENT,
    ReviewRequestError,
    _agent_prompt,
    chat_completions_url,
    model_name,
    run_review,
)


class Result:
    def __init__(self, status, result="", duration_ms=15, run_id="run-1"):
        self.status = status
        self.result = result
        self.duration_ms = duration_ms
        self.id = run_id


class RunReviewTests(unittest.TestCase):
    def test_missing_api_key_does_not_call_the_model(self):
        called = False

        def prompter(*_args, **_kwargs):
            nonlocal called
            called = True

        with mock.patch("sys.stderr", new_callable=io.StringIO):
            outcome = run_review("review", api_key="  ", prompter=prompter)
        self.assertEqual(outcome.exit_code, EXIT_STARTUP)
        self.assertIn("OPENADAPTOR_API_KEY", outcome.text)
        self.assertFalse(called)

    def test_finished_review_returns_the_text(self):
        def prompter(prompt, *, api_key):
            self.assertEqual(api_key, "key")
            self.assertIn("review this", prompt)
            return Result("finished", "### Summary\nLooks fine.\n")

        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            outcome = run_review("review this", api_key="key", prompter=prompter)
        self.assertEqual(outcome.exit_code, EXIT_OK)
        self.assertEqual(outcome.text, "### Summary\nLooks fine.\n")
        self.assertEqual(outcome.run_id, "run-1")
        self.assertIn("run_id=run-1", stderr.getvalue())
        self.assertNotIn("agent_id", stderr.getvalue())

    def test_run_error_exits_2(self):
        def prompter(*_args, **_kwargs):
            return Result("error", "blew up")

        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            outcome = run_review("review", api_key="key", prompter=prompter)
        self.assertEqual(outcome.exit_code, EXIT_RUN_FAILED)
        self.assertEqual(outcome.text, "The run did not finish.")
        self.assertEqual(outcome.run_id, "run-1")
        self.assertNotIn("Verdict", outcome.text)
        self.assertIn("blew up", stderr.getvalue())

    def test_request_error_exits_1(self):
        def prompter(*_args, **_kwargs):
            raise ReviewRequestError("OpenAdapter returned 401: invalid key")

        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            outcome = run_review("review", api_key="key", prompter=prompter)
        self.assertEqual(outcome.exit_code, EXIT_STARTUP)
        self.assertIn("invalid key", outcome.text)
        self.assertIn("invalid key", stderr.getvalue())
        self.assertNotIn("### Verdict", outcome.text)


class OpenAdapterRequestTests(unittest.TestCase):
    def test_request_uses_env_model_and_base_url(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"id":"chat-1","choices":[{"message":{"content":"### Summary\\nok"}}]}'

        env = {
            "OPENADAPTOR_BASE_URL": "https://example.test/v1/",
            "OPENADAPTOR_MODEL": "custom-model",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            with mock.patch("urllib.request.urlopen", return_value=Response()) as urlopen:
                result = _agent_prompt("review", api_key="key")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)
        body = json.loads(request.data.decode())
        self.assertEqual(body["model"], "custom-model")
        self.assertEqual(result.result, "### Summary\nok")

    def test_defaults_when_env_is_empty(self):
        with mock.patch.dict("os.environ", {"OPENADAPTOR_BASE_URL": "", "OPENADAPTOR_MODEL": ""}, clear=False):
            self.assertEqual(chat_completions_url(), DEFAULT_BASE_URL + "/chat/completions")
            self.assertEqual(model_name(), DEFAULT_MODEL)


if __name__ == "__main__":
    unittest.main()
