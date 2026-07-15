import asyncio
import qrcode
from telethon import TelegramClient
from telethon.tl.functions.auth import ExportLoginTokenRequest, AcceptLoginTokenRequest
import base64

API_ID = 20325617
API_HASH = "88e7d41871c1f736efe075bd51181789"
SESSION = "data/tg_session"

async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Вже авторизовано як: {me.first_name}")
        await client.disconnect()
        return

    print("Запитую QR-код...")
    token = await client(ExportLoginTokenRequest(api_id=API_ID, api_hash=API_HASH, except_ids=[]))

    token_b64 = base64.urlsafe_b64encode(token.token).decode()
    url = f"tg://login?token={token_b64}"

    qr = qrcode.QRCode()
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    print("\nВідскануй цей QR-код в Telegram на телефоні:")
    print("Налаштування → Пристрої → Підключити пристрій → Сканувати QR")
    print("\nЧекаю на підтвердження...")

    # Ждём подтверждения
    for _ in range(60):
        await asyncio.sleep(3)
        try:
            token2 = await client(ExportLoginTokenRequest(api_id=API_ID, api_hash=API_HASH, except_ids=[]))
            if hasattr(token2, 'user'):
                break
        except Exception as e:
            if 'SESSION_PASSWORD_NEEDED' in str(e):
                pwd = input("Введи пароль двофакторної авторизації: ")
                await client.sign_in(password=pwd)
                break

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\nАвторизовано як: {me.first_name} {me.last_name or ''}")
    else:
        print("Не вдалося авторизуватись")

    await client.disconnect()

asyncio.run(main())
