"""Standalone Telegram smoke test.

Uses python-telegram-bot (already in requirements.txt) instead of the old
`telebot` import, which was never declared as a dependency (issue 5.6).

This file matches `test_*.py`, so `unittest discover` imports it — importing it
must therefore do nothing: no `load_dotenv()` mutating the environment, no
message sent with the operator's real token. Sending is opt-in:

    python test_telegram.py --live

Without `--live` the script only prints that usage line.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

USAGE = (
    "test_telegram.py — no message sent.\n"
    "Sending contacts Telegram with the token from .env, so it is never done on\n"
    "import or by accident. To send one test message, run:\n"
    "    python test_telegram.py --live"
)


async def main():
    """Send one test message to the configured chat.

    Reads the token at call time (never at import) and never prints it.
    """
    load_dotenv()
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        print("❌ Missing TELEGRAM_TOKEN or CHAT_ID in .env")
        return
    bot = Bot(token)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text="✅ Test message from sniper bot!\nBot is working.",
        )
        print("✅ Message sent successfully! Check your Telegram.")
    except TelegramError as e:
        print(f"❌ Error sending message: {e}")


if __name__ == "__main__" and "--live" in sys.argv:
    asyncio.run(main())
elif __name__ == "__main__":
    print(USAGE)
