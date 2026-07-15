import asyncio
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

API_ID = 20325617
API_HASH = "88e7d41871c1f736efe075bd51181789"
PHONE = "+380930948792"
SESSION = "data/tg_session"

async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Вже авторизовано як: {me.first_name} (@{me.username or 'немає'})")
        await client.disconnect()
        return

    sent = await client.send_code_request(PHONE)
    print("Код відправлено. Якщо не прийшов — зараз запросимо дзвінок...")

    code = input("Введи код (або натисни Enter щоб отримати через дзвінок): ").strip()

    if not code:
        await client.send_code_request(PHONE, force_sms=True)
        print("Запит на дзвінок/SMS відправлено. Чекай дзвінка або SMS.")
        code = input("Введи код з дзвінка: ").strip()

    try:
        await client.sign_in(PHONE, code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        pwd = input("Введи пароль двофакторної авторизації: ")
        await client.sign_in(password=pwd)

    me = await client.get_me()
    print(f"Авторизовано як: {me.first_name} {me.last_name or ''} (@{me.username or 'немає'})")
    await client.disconnect()

asyncio.run(main())
