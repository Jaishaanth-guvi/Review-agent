import unittest

from pr_review.github import PullRequest
from pr_review.prompt import build_review_prompt, changed_files


def _pull(**overrides) -> PullRequest:
    data = {
        "owner": "acme",
        "repo": "widgets",
        "number": 12,
        "title": "Add login",
        "body": "Fixes the form",
        "url": "https://github.com/acme/widgets/pull/12",
        "author": "ada",
        "base_ref": "main",
        "head_ref": "login",
        "head_sha": "abc123",
        "diff": "diff --git a/src/login.py b/src/login.py\n+token = request.args['token']\n",
    }
    data.update(overrides)
    return PullRequest(**data)


class PromptTests(unittest.TestCase):
    def test_includes_metadata_and_files(self):
        prompt = build_review_prompt(_pull())
        self.assertIn("acme/widgets#12", prompt)
        self.assertIn("Add login", prompt)
        self.assertIn("src/login.py", prompt)
        self.assertIn("ready for approve", prompt)
        self.assertIn("`path`:line", prompt)
        self.assertIn("what the person does", prompt)
        self.assertIn("Do not edit files", prompt)
        self.assertNotIn("### Findings", prompt)

    def test_truncates_diff_and_keeps_file_list(self):
        diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            + ("+line\n" * 50)
            + "diff --git a/src/b.py b/src/b.py\n"
            + ("+more\n" * 50)
        )
        prompt = build_review_prompt(_pull(diff=diff), max_diff_chars=80)
        self.assertIn("src/a.py", prompt)
        self.assertIn("src/b.py", prompt)
        self.assertIn("truncated", prompt)
        self.assertIn("Dependency check was unavailable.", prompt)
        self.assertLess(prompt.index("```diff"), prompt.index("truncated") + 500)

    def test_changed_files_dedupes(self):
        diff = "diff --git a/src/a.py b/src/a.py\n\ndiff --git a/src/a.py b/src/a.py\n"
        self.assertEqual(changed_files(diff), ["src/a.py"])


if __name__ == "__main__":
    unittest.main()
