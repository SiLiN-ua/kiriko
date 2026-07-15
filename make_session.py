import asyncio,os
from telethon import TelegramClient
from dotenv import load_dotenv
load_dotenv(r"E:/Kiriko/.env")
async def main():
    c = TelegramClient(r"E:/Kiriko/data/tg_session", int(os.getenv("TG_API_ID")), os.getenv("TG_API_HASH"))
    await c.start(phone=os.getenv("TG_PHONE"))
    await c.disconnect()
asyncio.run(main())
