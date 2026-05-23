import os
from dotenv import load_dotenv
import telebot

load_dotenv()

TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

if not TOKEN or not CHAT_ID:
    print("❌ Missing TELEGRAM_TOKEN or CHAT_ID in .env")
else:
    bot = telebot.TeleBot(TOKEN)
    try:
        bot.send_message(CHAT_ID, "✅ Test message from sniper bot!\nBot is working.")
        print("✅ Message sent successfully! Check your Telegram.")
    except Exception as e:
        print(f"❌ Error sending message: {e}")
