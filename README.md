# pr-review-agent

A GitHub App webhook. When a pull request is opened or updated, it sends the diff to OpenAdapter and posts one comment on the pull request. A later push updates that comment.

The model sees the pull request title, description, file list, and unified diff. Diffs longer than 80000 characters are truncated. It does not read the repository checkout.

## Setup

Python 3.10 or newer, and an OpenAdapter API key.

```bash
uv venv --python 3.12
uv pip install -e .
```

```bash
export OPENADAPTOR_API_KEY="sk-cv-..."
export OPENADAPTOR_BASE_URL="https://api.openadapter.in/v1"
export OPENADAPTOR_MODEL="GLM-5"
export GITHUB_APP_ID="123456"
export GITHUB_APP_PRIVATE_KEY="/path/to/app.private-key.pem"
export GITHUB_WEBHOOK_SECRET="a long random string"
pr-review-listen
```

`OPENADAPTOR_BASE_URL` and `OPENADAPTOR_MODEL` are optional. They default to the values above. The review request goes to `{OPENADAPTOR_BASE_URL}/chat/completions`.

`GITHUB_APP_PRIVATE_KEY` may be a path to the PEM file, or the PEM text itself. The listener reviews `opened`, `synchronize`, and `ready_for_review`. It skips drafts.

## Docker

Docker Compose publishes the listener on port **8000**. That port stays the same after a restart. Put a hostname GitHub can reach in front of it:

```text
http://<hostname>:8000/webhook
```

```bash
docker compose up --build -d
```

The `review` logs print `GITHUB_WEBHOOK_SECRET`. Paste that into the GitHub App webhook secret. When the hostname changes, keep port `8000` and replace only the hostname.

Set `OPENADAPTOR_API_KEY`, `GITHUB_APP_ID`, and `GITHUB_APP_PRIVATE_KEY` in the environment or a `.env` file next to `docker-compose.yml` before pull requests can be reviewed. `OPENADAPTOR_BASE_URL` and `OPENADAPTOR_MODEL` can be set there too.

## GitHub App

Create the app under GitHub Settings, Developer settings, GitHub Apps:

- Webhook URL: `http://<hostname>:8000/webhook`
- Webhook secret: the value you will put in `GITHUB_WEBHOOK_SECRET`
- Permissions: Contents read, Pull requests read and write, Issues read and write, Metadata read
- Subscribe to the Pull requests event
- Generate a private key and note the App ID
- Install the app on the organization, for all repositories or a selected set

The comment starts with `<!-- pr-review-agent -->`. If the review does not finish, the comment says there is no verdict and includes the run id when one exists.
