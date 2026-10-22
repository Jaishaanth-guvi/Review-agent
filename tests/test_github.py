import json
import unittest
import urllib.error
from io import BytesIO
from unittest import mock

from pr_review.github import GitHubError, fetch_pull_request


class FetchTests(unittest.TestCase):
    def test_fetch_maps_json_and_diff(self):
        payload = {
            "title": "Add login",
            "body": "Fixes the form",
            "html_url": "https://github.com/acme/widgets/pull/12",
            "user": {"login": "ada"},
            "base": {"ref": "main"},
            "head": {"ref": "login", "sha": "abc123"},
        }

        def fake_urlopen(request, timeout):
            accept = request.get_header("Accept")
            if "diff" in accept:
                body = b"diff --git a/a.py b/a.py\n"
            else:
                body = json.dumps(payload).encode()
            return BytesIO(body)

        with mock.patch("pr_review.github.urllib.request.urlopen", fake_urlopen):
            pull = fetch_pull_request("acme", "widgets", 12, token="secret")

        self.assertEqual(pull.title, "Add login")
        self.assertEqual(pull.author, "ada")
        self.assertEqual(pull.head_sha, "abc123")
        self.assertIn("diff --git", pull.diff)

    def test_missing_pull_request(self):
        error = urllib.error.HTTPError(
            url="https://api.github.com/repos/acme/widgets/pulls/9",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=BytesIO(b'{"message":"Not Found"}'),
        )
        with mock.patch("pr_review.github.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GitHubError) as caught:
                fetch_pull_request("acme", "widgets", 9)
        self.assertIn("not found", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
