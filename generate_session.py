"""
Run locally on a trusted computer/Termux, NOT as the Railway web service.
The OTP and optional 2FA password are entered only in your local terminal.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("Telegram API ID: ").strip())
API_HASH = input("Telegram API HASH: ").strip()
PHONE = input("Telegram phone (international format): ").strip()

async def main():
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start(phone=PHONE)
    print("\nCopy this value into Railway as TELETHON_SESSION.\n")
    print(client.session.save())
    await client.disconnect()

asyncio.run(main())
