# pr-review-agent

A command-line review agent. It downloads a GitHub pull request, runs a local [Cursor SDK](https://cursor.com/docs/sdk/python) agent against a checkout with read-only tools, and prints the review to stdout.

The agent can read files in the workspace. It is not given edit or shell tools.

## Setup

Python 3.10 or newer, and a Cursor API key (user key or team service-account key) from the [Cursor dashboard](https://cursor.com/dashboard/integrations).

```bash
uv venv --python 3.12
uv pip install -e .
export CURSOR_API_KEY="cursor_..."
```

Private repositories also need `GITHUB_TOKEN` or `GH_TOKEN` with pull request read access.

## Usage

Run it from the repository you want the agent to read, or pass `--cwd`.

```bash
pr-review https://github.com/owner/repo/pull/123
pr-review owner/repo#123 --cwd /path/to/checkout
pr-review 123 --repo owner/repo
```

A bare number uses `--repo`, then `GITHUB_REPOSITORY`, then the `origin` remote of `--cwd`.

`--dry-run` prints the prompt and does not call the agent.

Stdout is the review. Progress and errors go to stderr.

```text
### Summary
...

### Findings
- **major**: `path` — problem, why it matters, and a fix.

### Verdict
request changes — ...
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The run finished. The review is on stdout. |
| 1 | The agent never started (missing key, auth, config, network). |
| 2 | The run started and did not finish (`error`, `cancelled`, or `expired`). |
| 3 | The pull request reference or GitHub download failed, or `--cwd` is missing. |

## What the agent sees

The prompt contains the pull request title, description, file list, and unified diff (truncated at `--max-diff-chars`, default 80000). The local checkout is context for surrounding code and may be on a different commit than the pull request. Project settings from that checkout's `.cursor/` directory are loaded. User, team, and plugin settings are not.
