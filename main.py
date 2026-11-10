import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, BufferedInputFile
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("relay")

def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

BOT_TOKEN = required("BOT_TOKEN")
OWNER_ID = int(required("OWNER_ID"))
PRIVATE_GROUP_ID = int(required("PRIVATE_GROUP_ID"))
BOT1_ID = int(required("BOT1_ID"))
API_ID = int(required("TELETHON_API_ID"))
API_HASH = required("TELETHON_API_HASH")
TELETHON_SESSION = required("TELETHON_SESSION")
DATABASE_URL = required("DATABASE_URL")
TIMEOUT = int(os.getenv("RESPONSE_TIMEOUT_SECONDS", "120"))
SETTLE = float(os.getenv("RESPONSE_SETTLE_SECONDS", "1.0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@").lower()

# Intentionally restricted to non-sensitive, general-purpose commands.
# Do not add phone/Aadhaar/PAN/address/vehicle-record lookup or SMS dispatch.
ALLOWED_COMMANDS = {"github", "noobgpt", "song", "image"}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
user_client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)
pool: Optional[asyncpg.Pool] = None

@dataclass
class Job:
    request_id: str
    public_chat_id: int
    public_message_id: int
    user_id: int
    command: str
    command_message_id: Optional[int] = None
    future: Optional[asyncio.Future] = None

queue: asyncio.Queue[Job] = asyncio.Queue()
active_job: Optional[Job] = None
latest_reply = None
reply_event = asyncio.Event()
worker_task: Optional[asyncio.Task] = None

def asyncpg_dsn_and_ssl(url: str):
    # asyncpg uses the ssl argument; strip libpq-only query parameters.
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k not in {"sslmode", "channel_binding"}]
    clean = urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))
    ssl = "require" if "sslmode=require" in url or p.hostname and (
        p.hostname.endswith("neon.tech") or "railway" in p.hostname
    ) else None
    return clean, ssl

async def init_db():
    global pool
    dsn, ssl = asyncpg_dsn_and_ssl(DATABASE_URL)
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5, ssl=ssl)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS relay_requests (
                request_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                public_chat_id BIGINT NOT NULL,
                public_message_id BIGINT NOT NULL,
                command TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                source_message_id BIGINT,
                error TEXT
            )
        """)

async def set_status(request_id: str, status: str, error: Optional[str] = None,
                     source_message_id: Optional[int] = None):
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE relay_requests
            SET status=$2, error=$3, source_message_id=$4,
                completed_at=CASE WHEN $2 IN ('completed','timeout','error') THEN NOW() ELSE completed_at END
            WHERE request_id=$1
        """, request_id, status, error, source_message_id)

def extract_command(text: str):
    match = re.match(r"^\s*/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", text or "", re.S)
    if not match:
        return None, None
    return match.group(1).lower(), (match.group(2) or "").strip()

def safe_json_summary(raw: str) -> Optional[str]:
    """Return only non-identifying status metadata from JSON, never raw records."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return "The upstream bot returned structured data. Raw JSON is not relayed."
    status = obj.get("api_status", obj.get("status", "completed"))
    count = obj.get("record_count")
    elapsed = obj.get("response_time")
    lines = ["✅ Request completed", f"Status: {str(status)[:60]}"]
    if isinstance(count, (int, float)):
        lines.append(f"Record count: {int(count)}")
    if isinstance(elapsed, (str, int, float)):
        lines.append(f"Response time: {str(elapsed)[:30]}")
    lines.append("Sensitive record fields are not forwarded.")
    return "\n".join(lines)

async def send_result(job: Job, message):
    text = (message.message or "").strip()
    # JSON responses are summarized; raw personal records are never broadcast.
    summary = safe_json_summary(text) if text.startswith("{") else None
    output = summary if summary is not None else text
    if output:
        await bot.send_message(
            job.public_chat_id,
            output[:4000],
            reply_to_message_id=job.public_message_id,
            disable_web_page_preview=True,
        )
    elif message.media:
        # Media forwarding is deliberately disabled in this safe starter.
        await bot.send_message(
            job.public_chat_id,
            "Bot 1 returned an attachment. Attachments are not automatically redistributed.",
            reply_to_message_id=job.public_message_id,
        )
    else:
        await bot.send_message(
            job.public_chat_id,
            "Bot 1 returned an unsupported response.",
            reply_to_message_id=job.public_message_id,
        )
    await set_status(job.request_id, "completed", source_message_id=message.id)

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Relay is online. Use an enabled command in this group.\n"
        "Available: /github, /noobgpt, /song, /image, /help"
    )

@dp.message()
async def handle_messages(message: Message):
    if not message.text:
        return

    command, args = extract_command(message.text)
    if not command:
        return

    # Owner login is deliberately handled as secure out-of-band session setup.
    if command == "login":
        if message.chat.type != "private" or not message.from_user or message.from_user.id != OWNER_ID:
            return
        await message.answer(
            "For account security, do not send Telegram OTPs or 2FA passwords to this bot. "
            "Generate a Telethon StringSession locally with generate_session.py, then set "
            "TELETHON_SESSION in Railway Variables and redeploy."
        )
        return

    if command == "help":
        await message.reply(
            "⚡ Enabled commands:\n"
            "• /github <username> — public GitHub profile\n"
            "• /noobgpt <prompt> — AI assistant\n"
            "• /song <prompt> — song generation\n"
            "• /image <prompt> — image generation\n"
            "• /help — this message\n\n"
            "Commands that retrieve private personal records or dispatch unsolicited SMS are not enabled."
        )
        return

    if command not in ALLOWED_COMMANDS:
        return
    if not args:
        await message.reply(f"Usage: /{command} <input>")
        return
    if not message.from_user:
        return

    # Accept commands in groups/supergroups; private-chat use is also supported.
    command_text = f"/{command} {args}"
    request_id = uuid.uuid4().hex
    job = Job(
        request_id=request_id,
        public_chat_id=message.chat.id,
        public_message_id=message.message_id,
        user_id=message.from_user.id,
        command=command_text,
    )
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO relay_requests
            (request_id,user_id,public_chat_id,public_message_id,command,status)
            VALUES($1,$2,$3,$4,$5,'queued')
        """, request_id, job.user_id, job.public_chat_id, job.public_message_id, job.command)
    await queue.put(job)
    await message.reply("⏳ Request queued. Waiting for the connected service…")

async def relay_worker():
    global active_job, latest_reply
    while True:
        job = await queue.get()
        try:
            active_job = job
            latest_reply = None
            reply_event.clear()
            await set_status(job.request_id, "sent")
            # The logged-in user account sends the command into the private group.
            sent = await user_client.send_message(PRIVATE_GROUP_ID, job.command)
            job.command_message_id = sent.id

            try:
                await asyncio.wait_for(reply_event.wait(), timeout=TIMEOUT)
                await asyncio.sleep(SETTLE)
                result = latest_reply
                if result is None:
                    raise asyncio.TimeoutError
                await send_result(job, result)
            except asyncio.TimeoutError:
                await bot.send_message(
                    job.public_chat_id,
                    f"⌛ No response received within {TIMEOUT} seconds.",
                    reply_to_message_id=job.public_message_id,
                )
                await set_status(job.request_id, "timeout", "Upstream response timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Relay request failed: %s", job.request_id)
            try:
                await bot.send_message(
                    job.public_chat_id,
                    "⚠️ The relay could not process this request. Please contact the bot owner.",
                    reply_to_message_id=job.public_message_id,
                )
            except Exception:
                log.exception("Could not send failure notice")
            await set_status(job.request_id, "error", "Internal relay error")
        finally:
            active_job = None
            latest_reply = None
            queue.task_done()

async def main():
    global worker_task
    await init_db()
    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise RuntimeError(
            "Telethon session is not authorized. Generate a StringSession locally "
            "and set TELETHON_SESSION in Railway."
        )

    @user_client.on(events.NewMessage(chats=PRIVATE_GROUP_ID, from_users=BOT1_ID, incoming=True))
    async def on_bot1_reply(event):
        global latest_reply
        job = active_job
        if job is None:
            return
        # Prefer Telegram reply-to correlation where Bot 1 uses it.
        reply_to = event.message.reply_to_msg_id
        if reply_to is not None and job.command_message_id is not None:
            if reply_to != job.command_message_id:
                return
        latest_reply = event.message
        reply_event.set()

    log.info("Telethon listener authorized; starting Bot 2 polling")
    worker_task = asyncio.create_task(relay_worker())
    try:
        await dp.start_polling(bot)
    finally:
        if worker_task:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        await user_client.disconnect()
        await bot.session.close()
        if pool:
            await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
