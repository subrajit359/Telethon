# Telegram Relay for Railway (restricted safe-command starter)

This service accepts a small allowlist of general-purpose commands in public groups,
queues them, sends them to a private Telegram group using an authorized Telethon
user account, listens for replies from Bot 1, and posts the response back in the
original public group. PostgreSQL stores request status.

## Safety scope
Enabled commands: `/github`, `/noobgpt`, `/song`, `/image`, `/help`.
It intentionally does not relay commands for private personal records (phone,
Aadhaar, PAN, addresses, vehicle records, etc.) or SMS dispatch. If Bot 1 returns
JSON, only non-identifying status metadata is posted; raw JSON/data records are
not redistributed. Attachments are not automatically redistributed.

## Railway variables
Set all values in `.env.example` as Railway service variables. `DATABASE_URL`
must point to an accessible PostgreSQL database. For Neon, use the connection
string from Neon; the app requests TLS for Neon hosts.

- `BOT_TOKEN`: Bot 2 token
- `OWNER_ID`: numeric Telegram ID authorized for owner-only `/login` instructions
- `PRIVATE_GROUP_ID`: numeric private group chat ID (usually starts with `-100`)
- `BOT1_ID`: numeric user ID of Bot 1
- `TELETHON_API_ID`, `TELETHON_API_HASH`: from my.telegram.org
- `TELETHON_SESSION`: StringSession generated locally
- `DATABASE_URL`: PostgreSQL DSN
- `RESPONSE_TIMEOUT_SECONDS`: default 120
- `RESPONSE_SETTLE_SECONDS`: default 1.0

## Generate the Telethon session
On a trusted computer or Android Termux:
1. `pip install -r requirements.txt`
2. `python generate_session.py`
3. Enter API ID, API hash, phone, OTP, and 2FA password if prompted.
4. Copy the resulting session string into Railway as `TELETHON_SESSION`.
5. Never commit or share the session string.

The bot deliberately does not collect Telegram OTPs in chat. `/login` in the
owner's private chat explains this secure setup.

## Deploy
1. Push this folder to a private GitHub repository.
2. Create a Railway project from that repository.
3. Add the environment variables.
4. Deploy. The Dockerfile starts `python main.py`.
5. Add Bot 2 to each public group with permission to send messages.
6. Add the Telethon account to the private group. Ensure it can send commands
   and read Bot 1's replies.
7. Disable Bot 2's group privacy in BotFather if you need it to see ordinary
   group messages. Explicit slash commands are generally visible to bots, but
   group permissions and command addressing still matter.

## Reply matching limitation
Only one request is sent to Bot 1 at a time. This prevents ordinary overlapping
requests. If Bot 1 replies to the exact command message, the app verifies the
reply-to message ID. If Bot 1 does not use reply-to, the app associates the next
new message from Bot 1 with the active request. A delayed response from an older
request could therefore be misattributed; strict correlation requires Bot 1 to
reply to the command message or include a request identifier.

## Important
This is a deployment-ready starter, not a guarantee against platform, permission,
credential, or Bot 1 behavior differences. Test first with one harmless command.
