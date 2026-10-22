#!/bin/sh
set -eu

secret_file="${WEBHOOK_SECRET_FILE:-/var/lib/pr-review/webhook-secret}"

if [ -z "${GITHUB_WEBHOOK_SECRET:-}" ] && [ -f "$secret_file" ]; then
  GITHUB_WEBHOOK_SECRET=$(cat "$secret_file")
  export GITHUB_WEBHOOK_SECRET
fi

if [ -z "${GITHUB_WEBHOOK_SECRET:-}" ]; then
  mkdir -p "$(dirname "$secret_file")"
  GITHUB_WEBHOOK_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
  printf "%s" "$GITHUB_WEBHOOK_SECRET" > "$secret_file"
  export GITHUB_WEBHOOK_SECRET
fi

port="${PORT:-8000}"
echo "GITHUB_WEBHOOK_SECRET=${GITHUB_WEBHOOK_SECRET}"
echo "Webhook URL: http://<hostname>:${port}/webhook"
exec pr-review-listen --host "${HOST:-0.0.0.0}" --port "${port}"
