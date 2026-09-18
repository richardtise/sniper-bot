"""Standalone Telegram smoke test.

Uses python-telegram-bot (already in requirements.txt) instead of the old
`telebot` import, which was never declared as a dependency (issue 5.6).

Run with: python test_telegram.py
"""

import asyncio
import os

from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")


async def main():
    if not TOKEN or not CHAT_ID:
        print("❌ Missing TELEGRAM_TOKEN or CHAT_ID in .env")
        return
    bot = Bot(TOKEN)
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text="✅ Test message from sniper bot!\nBot is working.",
        )
        print("✅ Message sent successfully! Check your Telegram.")
    except TelegramError as e:
        print(f"❌ Error sending message: {e}")


if __name__ == "__main__":
    asyncio.run(main())
