import json
import unittest
import urllib.error
from io import BytesIO
from unittest import mock

from pr_review.github import (
    GitHubError,
    fetch_pull_request,
    parse_github_remote,
    resolve_pull_request_ref,
)


class ParseRemoteTests(unittest.TestCase):
    def test_https_and_ssh(self):
        self.assertEqual(
            parse_github_remote("https://github.com/acme/widgets.git"),
            ("acme", "widgets"),
        )
        self.assertEqual(
            parse_github_remote("git@github.com:acme/widgets.git"),
            ("acme", "widgets"),
        )

    def test_non_github(self):
        self.assertIsNone(parse_github_remote("https://gitlab.com/acme/widgets.git"))


class ResolveRefTests(unittest.TestCase):
    def test_url(self):
        self.assertEqual(
            resolve_pull_request_ref("https://github.com/acme/widgets/pull/12/files"),
            ("acme", "widgets", 12),
        )

    def test_short_forms(self):
        self.assertEqual(
            resolve_pull_request_ref("acme/widgets#12"),
            ("acme", "widgets", 12),
        )
        self.assertEqual(
            resolve_pull_request_ref("acme/widgets/pull/12"),
            ("acme", "widgets", 12),
        )

    def test_number_uses_repo_flag_before_remote(self):
        self.assertEqual(
            resolve_pull_request_ref(
                "12",
                repo="acme/widgets",
                remote_url="git@github.com:other/repo.git",
            ),
            ("acme", "widgets", 12),
        )

    def test_number_uses_github_repository_then_remote(self):
        self.assertEqual(
            resolve_pull_request_ref("12", github_repository="acme/widgets"),
            ("acme", "widgets", 12),
        )
        self.assertEqual(
            resolve_pull_request_ref("12", remote_url="https://github.com/acme/widgets.git"),
            ("acme", "widgets", 12),
        )

    def test_number_without_repo(self):
        with self.assertRaises(GitHubError):
            resolve_pull_request_ref("12", remote_url=None, github_repository=None)

    def test_repo_flag_must_match_url(self):
        with self.assertRaises(GitHubError):
            resolve_pull_request_ref(
                "https://github.com/acme/widgets/pull/12",
                repo="other/repo",
            )

    def test_rejects_unknown_text(self):
        with self.assertRaises(GitHubError):
            resolve_pull_request_ref("not a pr")


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
