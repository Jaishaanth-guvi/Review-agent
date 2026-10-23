"""Find files that depend on a change, and tests that mention it."""

from __future__ import annotations

import re
from pathlib import Path

MAX_DEPENDENTS = 20
MAX_TESTS = 10
_MAX_FILE_BYTES = 200_000
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".tools",
}
_IMPORT_LINE = re.compile(r"(?i)\b(import|require|include|from)\b")
_TEST_DIR = {"test", "tests", "__tests__", "spec", "specs"}


def dependency_note(root: str, changed_paths: list[str]) -> str:
    """Return a short note of dependents and related tests under ``root``."""
    changed = [path for path in changed_paths if path and path != "/dev/null"]
    stems = {Path(path).stem for path in changed if len(Path(path).stem) >= 2}
    names = {Path(path).name for path in changed}
    dependents: list[str] = []
    tests: list[str] = []
    seen_dependents: set[tuple[str, int]] = set()
    seen_tests: set[str] = set()

    for path in changed:
        if _is_test_path(path) and path not in seen_tests and len(tests) < MAX_TESTS:
            tests.append(f"- {path}: changed in this pull request")
            seen_tests.add(path)

    base = Path(root)
    if base.is_dir():
        for file_path in _source_files(base):
            relative = file_path.relative_to(base).as_posix()
            if relative in changed:
                continue
            text = _read_text(file_path)
            if text is None:
                continue
            if _is_test_path(relative):
                _collect_test(relative, text, stems, names, tests, seen_tests)
            else:
                _collect_dependent(relative, text, stems, names, dependents, seen_dependents)
            if len(dependents) >= MAX_DEPENDENTS and len(tests) >= MAX_TESTS:
                break

    dependent_block = "\n".join(dependents) if dependents else "None."
    if tests:
        test_block = "\n".join(tests)
    else:
        test_block = "No test for the change was found."
    return f"Dependents:\n{dependent_block}\n\nTests:\n{test_block}\n"


def _source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def _collect_dependent(
    relative: str,
    text: str,
    stems: set[str],
    names: set[str],
    dependents: list[str],
    seen: set[tuple[str, int]],
) -> None:
    if len(dependents) >= MAX_DEPENDENTS:
        return
    for number, line in enumerate(text.splitlines(), start=1):
        if (relative, number) in seen:
            continue
        if not _IMPORT_LINE.search(line):
            continue
        if not _mentions(line, stems, names):
            continue
        seen.add((relative, number))
        dependents.append(f"- {relative}:{number}: {line.strip()[:180]}")
        if len(dependents) >= MAX_DEPENDENTS:
            return


def _collect_test(
    relative: str,
    text: str,
    stems: set[str],
    names: set[str],
    tests: list[str],
    seen: set[str],
) -> None:
    if relative in seen or len(tests) >= MAX_TESTS:
        return
    for number, line in enumerate(text.splitlines(), start=1):
        if _mentions(line, stems, names):
            tests.append(f"- {relative}:{number}: {line.strip()[:180]}")
            seen.add(relative)
            return


def _mentions(line: str, stems: set[str], names: set[str]) -> bool:
    if any(name in line for name in names):
        return True
    for stem in stems:
        if re.search(rf"\b{re.escape(stem)}\b", line):
            return True
    return False


def _is_test_path(path: str) -> bool:
    pure = Path(path)
    if any(part in _TEST_DIR for part in pure.parts):
        return True
    name = pure.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def _read_text(path: Path) -> str | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > _MAX_FILE_BYTES:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")
