import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-relay")

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"])
BOT1_ID = int(os.environ["BOT1_ID"])
API_ID = int(os.environ["TELETHON_API_ID"])
API_HASH = os.environ["TELETHON_API_HASH"]
PHONE = os.getenv("TELETHON_PHONE")
DATABASE_URL = os.environ["DATABASE_URL"]
TIMEOUT = float(os.getenv("RESPONSE_TIMEOUT_SECONDS", "15"))
SETTLE = float(os.getenv("RESPONSE_SETTLE_SECONDS", "1"))

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
client = TelegramClient("telethon", API_ID, API_HASH)
pool: Optional[asyncpg.Pool] = None

@dataclass
class Job:
    request_id: str
    chat_id: int
    user_id: int
    command: str
    future: asyncio.Future

jobs: asyncio.Queue[Job] = asyncio.Queue()
# Only one request is sent to Bot 1 at a time. This avoids mixing replies
# when Bot 1 does not reply-to the exact command message.
active_job: Optional[Job] = None
active_lock = asyncio.Lock()
latest_reply: Optional[object] = None
reply_event = asyncio.Event()

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS relay_requests (
                request_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                command TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                result_message_id BIGINT,
                error TEXT
            )
        """)

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer("Send the command you want forwarded to the connected bot.\nExample: /getdata 123")

@dp.message(F.text)
async def receive_command(message: Message):
    if message.chat.type != "private":
        return
    command = (message.text or "").strip()
    if not command:
        return
    request_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    job = Job(request_id, message.chat.id, message.from_user.id, command, future)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO relay_requests(request_id,user_id,chat_id,command,status) VALUES($1,$2,$3,$4,'queued')",
            request_id, message.from_user.id, message.chat.id, command
        )
    await jobs.put(job)
    await message.answer("⏳ Request received. Waiting for the result…")

async def worker():
    global active_job, latest_reply
    while True:
        job = await jobs.get()
        try:
            async with active_lock:
                active_job = job
                latest_reply = None
                reply_event.clear()
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE relay_requests SET status='sent' WHERE request_id=$1", job.request_id)
                # Bot 2 posts the command into the group. The Telethon account
                # must be a member of that group and able to see Bot 1's replies.
                await bot.send_message(GROUP_ID, job.command)
                try:
                    await asyncio.wait_for(reply_event.wait(), timeout=TIMEOUT)
                    # Allow a short burst of edits/follow-up messages to settle.
                    await asyncio.sleep(SETTLE)
                    result = latest_reply
                    if result is None:
                        raise asyncio.TimeoutError
                    await deliver(job, result)
                except asyncio.TimeoutError:
                    await bot.send_message(job.chat_id, "⌛ Bot 1 did not reply within 15 seconds. Please try again.")
                    async with pool.acquire() as conn:
                        await conn.execute("UPDATE relay_requests SET status='timeout', error='Bot 1 response timeout' WHERE request_id=$1", job.request_id)
        except Exception:
            log.exception("Request %s failed", job.request_id)
            try:
                await bot.send_message(job.chat_id, "⚠️ Something went wrong while processing your request.")
            except Exception:
                pass
            async with pool.acquire() as conn:
                await conn.execute("UPDATE relay_requests SET status='error', error='Internal processing error' WHERE request_id=$1", job.request_id)
        finally:
            active_job = None
            jobs.task_done()

async def deliver(job: Job, event_message):
    # Forward the latest Bot 1 message as-is when possible, preserving media.
    # For text-only replies, copy_message is not available across user sessions;
    # send text directly. Media is downloaded by Telethon and uploaded by Bot 2.
    if event_message.message:
        await bot.send_message(job.chat_id, event_message.message)
    elif event_message.media:
        data = await event_message.download_media(file=bytes)
        if data:
            from aiogram.types import BufferedInputFile
            filename = "result.bin"
            if event_message.photo:
                filename = "result.jpg"
            elif event_message.document:
                filename = event_message.file.name or "result.bin"
            await bot.send_document(job.chat_id, BufferedInputFile(data, filename=filename))
        else:
            await bot.send_message(job.chat_id, "Bot 1 replied with media, but it could not be downloaded.")
    else:
        await bot.send_message(job.chat_id, "Bot 1 sent a reply that this relay cannot display.")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE relay_requests SET status='completed', completed_at=NOW(), result_message_id=$2 WHERE request_id=$1",
            job.request_id, event_message.id
        )

async def main():
    await init_db()
    await client.start(phone=PHONE)
    log.info("Telethon listener connected")

    @client.on(events.NewMessage(chats=GROUP_ID, from_users=BOT1_ID, incoming=True))
    async def on_bot1_reply(event):
        global latest_reply
        if active_job is None:
            log.info("Ignoring Bot 1 message: no active request")
            return
        latest_reply = event.message
        reply_event.set()

    worker_task = asyncio.create_task(worker())
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await client.disconnect()
        await bot.session.close()
        if pool:
            await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
