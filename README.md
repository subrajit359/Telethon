# Telegram Bot Relay (Bot 2 → group → Bot 1 → Bot 2)

Python starter using Aiogram, Telethon and PostgreSQL. It queues user requests and sends them **one at a time** to the group so Bot 1 replies cannot be mixed between concurrent users. It forwards the latest message received from the configured Bot 1 within the 15-second response window.

## Important Telegram constraints

- Bot 2 can send a message to the group if it is a member and has permission.
- The Bot API bot generally cannot read another bot's messages. A dedicated Telegram user account logged in through Telethon listens for Bot 1's replies.
- The Telethon account must be a member of the group and must be allowed to see Bot 1's messages.
- This starter assumes Bot 1's response is a new message from the configured `BOT1_ID`. If Bot 1 edits an existing message instead of sending a new one, or responds in a thread/topic, the listener needs adjustment.
- Only one command is in flight at a time. This is deliberate: if Bot 1 does not reply-to the command message, there is no reliable way to correlate concurrent requests from group traffic alone.
- Use this only with accounts, bots, group content and data you are authorized to access. Protect the Telethon session file like a password.

## Setup

1. Create Bot 2 with @BotFather and add it to the private group. Give it permission to send messages.
2. Add a dedicated Telegram user account to the group. This account will be used by Telethon to read Bot 1's replies.
3. Get `api_id` and `api_hash` from https://my.telegram.org.
4. Find the numeric group ID and Bot 1's numeric user ID.
5. Copy `.env.example` to `.env` and fill in every value. Change the PostgreSQL password in both `.env` and `docker-compose.yml`.
6. Start it:

   ```bash
   docker compose up --build -d
   ```

7. On first Telethon login, inspect logs and complete the interactive login/code/2FA prompt. For production, create the Telethon session once interactively and then run the service with the saved session file mounted persistently. Do not share the `.session` file.

## First-run Telethon login note

The default container command is non-interactive. The cleanest deployment is to create the Telethon session on a trusted machine first using the same API ID/hash and session name `telethon`, then securely copy `telethon.session` into this project directory before starting Compose. Never commit `.env` or `telethon.session` to Git.

## Current behavior / limitations

- Text replies are delivered as text; media is delivered as a document attachment.
- The listener captures the first new message from Bot 1 during the active request, then waits the settle interval and uses the latest captured message.
- If Bot 1 emits several messages, the final one within the settle period is delivered. Increase `RESPONSE_SETTLE_SECONDS` if Bot 1 sends a burst.
- A 15-second timeout is configurable with `RESPONSE_TIMEOUT_SECONDS`.
- Database rows record request status and errors. Add authentication, rate limits, admin controls and privacy/retention rules before opening the bot publicly.
