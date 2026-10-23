import tempfile
import unittest
from pathlib import Path

from pr_review.dependencies import dependency_note


class DependencyNoteTests(unittest.TestCase):
    def test_finds_a_dependent_and_a_test(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / "src").mkdir()
            (base / "tests").mkdir()
            (base / "src" / "login.py").write_text("def login():\n    return True\n", encoding="utf-8")
            (base / "src" / "app.py").write_text(
                "from src.login import login\n\nlogin()\n",
                encoding="utf-8",
            )
            (base / "tests" / "test_login.py").write_text(
                "from src.login import login\n\ndef test_login():\n    assert login()\n",
                encoding="utf-8",
            )
            note = dependency_note(root, ["src/login.py"])

        self.assertIn("src/app.py:1:", note)
        self.assertIn("from src.login import login", note)
        self.assertIn("tests/test_login.py:1:", note)
        self.assertNotIn("No test for the change was found.", note)

    def test_says_when_no_test_mentions_the_change(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / "src").mkdir()
            (base / "src" / "login.py").write_text("def login():\n    return True\n", encoding="utf-8")
            note = dependency_note(root, ["src/login.py"])

        self.assertIn("Dependents:\nNone.", note)
        self.assertIn("No test for the change was found.", note)

    def test_notes_a_test_file_included_in_the_diff(self):
        note = dependency_note("/tmp/pr-review-missing-checkout", ["tests/test_login.py"])
        self.assertIn("tests/test_login.py: changed in this pull request", note)


if __name__ == "__main__":
    unittest.main()
