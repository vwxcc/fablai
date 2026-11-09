# fablai

A Telegram-first multi-step AI agent. The end-user surface is deliberately minimal: users send text, documents or voice messages and receive a processing status followed by the result. All provider credentials, integrations, limits and feature switches live in the protected admin console.

## Features

- Multi-step OpenAI-compatible agent with bounded tool execution.
- Telegram-native HTML rendering, safe message splitting, code blocks, quotes, spoilers and aligned text tables.
- Separate provider pools for generation, page-fetch extraction, search, TTS, speech-to-text, sandbox runners and custom connectors.
- Multiple connections per task type, priority order, enable/disable flags, per-connection concurrency and rolling request limits.
- Default user allowance: **1,000,000 estimated/reported tokens per 12 hours**.
- Temporary per-request upload workspace. User files are deleted after processing; no permanent file object store is used.
- Persistent chat history and revocable share links stored in the database (sharing exposes text history only, never temporary uploads).
- Admin-only web console; no user settings panel, inline keyboards or tool buttons in Telegram.

## Requirements

Python 3.12+. Configure a Telegram bot with BotFather, then set the environment variables from the .env.example file.

## Local run

~~~bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
~~~

The service runs Telegram webhook mode when TELEGRAM_WEBHOOK_URL is set. Without it, the bot starts long polling (useful locally). On Render, use one web service and set TELEGRAM_WEBHOOK_URL to https://YOUR-SERVICE.onrender.com/telegram/webhook.

## Required environment

- TELEGRAM_BOT_TOKEN: token issued by BotFather.
- ADMIN_TELEGRAM_IDS: comma-separated Telegram numeric IDs allowed to use admin commands.
- ADMIN_PASSWORD: password for the separate web admin console.
- APP_SECRET: long random secret (32+ characters) used for sessions, share tokens and encryption-key derivation.
- DATABASE_URL: persistent PostgreSQL URL recommended for deployment; SQLite is supported for local development.
- TELEGRAM_WEBHOOK_SECRET: random webhook secret (required when webhook mode is used).

No API key is required in environment variables: providers are configured and encrypted in the admin console. The primary generation model is configured as one simple base URL + API key + model. Auxiliary task types have separate connection pools.

## Admin

Open /admin, authenticate with ADMIN_PASSWORD, and configure the primary generation API. Add auxiliary connections for Fetch, Search, TTS, STT, Sandbox or custom REST connectors as needed. Secrets are encrypted before being stored in the database and are never returned by admin API reads.

Admin access is also available to configured Telegram IDs through /admin in a private chat; the bot returns the admin URL only to allowlisted IDs.

## Telegram behaviour

- A normal message starts or continues the user's private conversation.
- /new starts a new conversation.
- /share creates a text-only link to the current conversation.
- Voice messages are transcribed only when STT is enabled.
- Documents are placed in a temporary request directory and are removed after the agent finishes.
- During work, the bot edits one status message with a short phase label. No progress buttons, emoji or tool controls are shown.

## Safety and operational notes

- The agent has configurable step and runtime budgets; tools are allow-listed and their arguments validated.
- URL fetching blocks localhost, private, reserved and non-HTTP destinations, revalidates redirects and caps response size.
- Sandbox execution is disabled unless an authenticated external runner connection is configured. Never run untrusted code inside the web process.
- Share links expose conversation text only. They do not expose API keys, admin data, temporary uploads or sandbox workspaces.
- Production deployments should use PostgreSQL. A local SQLite file or ephemeral container filesystem is not durable across restarts.
- Uploads are intentionally not backed up or retained. If the service is restarted, in-flight temporary uploads are lost.

## Health

GET /healthz returns a minimal status response. The webhook endpoint is POST /telegram/webhook.
