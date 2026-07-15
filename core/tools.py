"""
Kiriko — Tool wrappers
Каждая функция вызывает нужный инструмент и возвращает строку с результатом.

Многопоточность:
  - Flask работает в threaded=True → несколько Flask-потоков параллельно
  - Claude API вызовы: полностью параллельны (SDK thread-safe)
  - Telegram боты: один выделенный event loop в отдельном потоке (_tg_loop)
    Все корутины отправляются туда через run_coroutine_threadsafe().
    _tg_use_lock (threading.Lock) гарантирует что одновременно идёт
    только одна беседа с ботом (Telethon не поддерживает параллельные сессии).
"""
import os
import sys
import io
import json
import asyncio
import threading
import re
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

# Telegram bot identifiers — set in .env
_BOT_NEMEZIDA    = os.getenv('TG_BOT_NEMEZIDA', '')
_BOT_SHERLOCK    = os.getenv('TG_BOT_SHERLOCK', '')
_BOT_FANSTATISTIK = os.getenv('TG_BOT_FANSTATISTIK', '')

# Корінь проекту Kiriko (два рівні вгору від core/tools.py)
_PROJECT_ROOT  = os.path.dirname(os.path.dirname(__file__))
PYTHON         = sys.executable
_REPORTS_DIR   = os.path.join(_PROJECT_ROOT, 'static', 'reports')
os.makedirs(_REPORTS_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — выделенный event loop в фоновом потоке
# ══════════════════════════════════════════════════════════════════════════════

# Один event loop на весь процесс — все Telegram корутины крутятся здесь
_tg_loop = asyncio.new_event_loop()
threading.Thread(
    target=_tg_loop.run_forever,
    daemon=True,
    name='kiriko-tg-loop',
).start()

_tg_client    = None
_tg_init_lock = threading.Lock()  # для однократной инициализации клиента
_tg_use_lock  = threading.Lock()  # сериализация бесед: одна за раз


async def _init_tg_client_async():
    """Создаём и запускаем Telethon-клиент внутри выделенного event loop."""
    from telethon import TelegramClient
    session = os.path.join(_PROJECT_ROOT, 'data', 'tg_session')
    client = TelegramClient(
        session,
        int(os.getenv('TG_API_ID')),
        os.getenv('TG_API_HASH'),
    )
    await client.start(phone=os.getenv('TG_PHONE'))
    return client


def _ensure_tg_client():
    """Инициализирует клиент один раз. Потокобезопасно."""
    global _tg_client
    if _tg_client is not None:
        return
    with _tg_init_lock:
        if _tg_client is not None:
            return
        fut = asyncio.run_coroutine_threadsafe(_init_tg_client_async(), _tg_loop)
        _tg_client = fut.result(timeout=60)


def _run_tg(coro, timeout: int = 360):
    """
    Отправляет корутину в выделенный Telegram event loop и ждёт результата.
    _tg_use_lock гарантирует что одновременно работает только одна беседа.
    """
    _ensure_tg_client()
    with _tg_use_lock:
        fut = asyncio.run_coroutine_threadsafe(coro, _tg_loop)
        return fut.result(timeout=timeout)


IGNORE_PHRASES = [
    "запит надіслано", "зачекайте", "будь ласка", "це може зайняти",
    "пожалуйста подождите", "запрос отправлен", "поиск займёт",
    "выполняется", "processing", "please wait", "ждіть", "ожидайте",
    "вычисляю", "вычисл", "обраховую", "розраховую",
    "розпізнано",  # Nemezida: "Розпізнано як: Прізвище - ..."
]

_DATE_RE = re.compile(r'\d{2}\.\d{2}\.\d{4}')
_ERROR_WORDS = ("не знайдено", "помилка", "відсутн", "error", "not found",
                "невірний", "невірно", "відмов")

def _is_loading(text: str) -> bool:
    return any(p in text.lower() for p in IGNORE_PHRASES)

def _is_nemezida_echo(text: str) -> bool:
    """
    Nemezida для ФИО+ДН запитів надсилає коротке повідомлення-відлуння
    з розпізнаним іменем та датою (наприклад: "Зернова Юліана Андріївна 14.05.1991")
    перед тим як надіслати PDF. Це НЕ результат — треба ігнорувати.
    Ознаки: короткий текст (<120 символів) + містить дату + НЕ містить слів помилки.
    """
    if len(text) >= 120:
        return False
    if not _DATE_RE.search(text):
        return False
    tl = text.lower()
    if any(w in tl for w in _ERROR_WORDS):
        return False  # справжня помилка — не ігноруємо
    return True


# ─── Базовый запрос к Telegram-боту ──────────────────────────────────────────
async def _tg_bot_query(bot: str, query: str,
                        wait_rounds: int = 24, interval: int = 5) -> str:
    """
    Отправляет query боту, ждёт ответа.
    Вызывается через _run_tg() — asyncio.Lock не нужен,
    сериализация обеспечена threading.Lock в _run_tg.
    """
    msgs_before = await _tg_client.get_messages(bot, limit=1)
    last_id = msgs_before[0].id if msgs_before else 0
    await _tg_client.send_message(bot, query)

    for _ in range(wait_rounds):
        await asyncio.sleep(interval)
        msgs = await _tg_client.get_messages(bot, limit=20)
        new = sorted([m for m in msgs if m.id > last_id], key=lambda m: m.id)
        parts = []
        for m in new:
            text = (m.message or '').strip()
            if text and not _is_loading(text) and len(text) > 10:
                parts.append(text)
        if parts:
            return "\n\n".join(parts)
    return "Нічого не знайдено (таймаут)"


# ─── ThunderTrace ─────────────────────────────────────────────────────────────
_thunder_lock = threading.Lock()

def thundertrace_search(username: str) -> str:
    """Поиск username по 350+ платформам."""
    try:
        with _thunder_lock:
            from thundertrace.core.checker import ThunderChecker
            checker = ThunderChecker(username)
            results = asyncio.run(checker.check_all())

        found = [r for r in results if r.get('found')]
        if not found:
            return f"[ThunderTrace] Ничего не найдено для «{username}»"
        lines = [f"[ThunderTrace] Найдено {len(found)} платформ для «{username}»:"]
        for r in found[:30]:
            lines.append(f"  • {r.get('name','?')}: {r.get('url','')}")
        if len(found) > 30:
            lines.append(f"  ... и ещё {len(found)-30}")
        return "\n".join(lines)
    except Exception as e:
        return f"[ThunderTrace] Ошибка: {e}"


# ─── Nemezida ─────────────────────────────────────────────────────────────────

def _extract_pdf_text_xml(doc) -> str:
    """
    Читає текст з pymupdf-документу через XML-режим.
    Потрібно тому що Nemezida-PDF використовують шрифт Arial з Identity-H кодуванням
    (CID fonts) — get_text() повертає кракозябри, але XML-режим правильно
    декодує символи через Unicode-сутності.
    """
    lines = []
    for page in doc:
        xml_str = page.get_text("xml")
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            # якщо XML зламаний — fallback на дефолт
            lines.append(page.get_text())
            continue
        for line_el in root.iter("line"):
            t = line_el.get("text", "").strip()
            if t:
                lines.append(t)
    return "\n".join(lines).strip()


async def _tg_nemezida_query(query: str) -> str:
    """
    Запрос к SmartSearchcloseBot (Nemezida).
    Расписание ожидания точно как в osint-hub:
      Фаза 1: каждые 5 сек × 24 = ~2 мин
      Фаза 2: каждые 15 сек × 52 = ~13 мин
      Итого максимум ~15 минут.
    Бот присылает PDF-файл — определяем через m.file (как в osint-hub),
    скачиваем в BytesIO, читаем текст через pymupdf.
    """
    bot = _BOT_NEMEZIDA
    msgs_before = await _tg_client.get_messages(bot, limit=1)
    last_id = msgs_before[0].id if msgs_before else 0
    await _tg_client.send_message(bot, query)

    # Підготовка фільтра: для запитів без літер (ІПН/телефон) — перевіряємо префікс файлу
    query_str    = str(query).strip()
    query_digits = re.sub(r'\D', '', query_str)
    has_letters  = bool(re.search(r'[a-zA-Zа-яА-ЯіІїЇєЄ]', query_str))
    # Нормалізуємо телефон: 380XXXXXXXXX → 0XXXXXXXXX
    normalized_digits = query_digits
    if query_digits.startswith('380') and len(query_digits) == 12:
        normalized_digits = '0' + query_digits[3:]

    def _pdf_matches_query(fname: str) -> bool:
        """Чи відповідає ім'я PDF-файлу поточному запиту (щоб не брати чужий PDF)."""
        if has_letters or not query_digits:
            return True  # Для ФИО+ДН фільтр не потрібен — беремо будь-який новий PDF
        fname_prefix = fname.split('_')[0] if '_' in fname else ''
        if not fname_prefix:
            return True
        return (fname_prefix.startswith(normalized_digits)
                or fname_prefix.startswith(query_digits))

    async def _try_download_pdf(m) -> str | None:
        """Скачати та розпарсити PDF з повідомлення. Повертає текст або None."""
        import time
        fname = getattr(m.file, 'name', '') or ''
        mime  = getattr(m.file, 'mime_type', '') or ''
        is_pdf = (mime == 'application/pdf' or fname.lower().endswith('.pdf'))
        if not is_pdf:
            return f"[Файл отримано: {fname}]"
        if not _pdf_matches_query(fname):
            return None  # Чужий PDF — пропускаємо
        buf = io.BytesIO()
        await _tg_client.download_media(m, file=buf)
        buf.seek(0)
        pdf_bytes = buf.read()

        # Зберігаємо PDF для скачування в чаті
        safe_q = re.sub(r'[^a-zA-Z0-9_]', '_', query)[:30]
        pdf_filename = f"nemezida_{int(time.time())}_{safe_q}.pdf"
        pdf_path = os.path.join(_REPORTS_DIR, pdf_filename)
        try:
            with open(pdf_path, 'wb') as f:
                f.write(pdf_bytes)
            download_url = f"/static/reports/{pdf_filename}"
        except Exception:
            download_url = None

        try:
            import pymupdf
            doc  = pymupdf.open(stream=io.BytesIO(pdf_bytes), filetype='pdf')
            text = _extract_pdf_text_xml(doc)
            if download_url:
                return f"[NEMEZIDA_PDF_URL:{download_url}]\n{text}" if text else f"[NEMEZIDA_PDF_URL:{download_url}]"
            return text if text else None
        except Exception as pdf_err:
            return f"[PDF parse error] {pdf_err}"

    schedule = [(5, 24), (15, 52)]
    for interval, count in schedule:
        for _ in range(count):
            await asyncio.sleep(interval)
            msgs = await _tg_client.get_messages(bot, limit=10)
            new_msgs = sorted(
                [m for m in msgs if m.id > last_id],
                key=lambda x: x.id
            )
            if not new_msgs:
                continue

            # ── ПРОХІД 1: шукаємо PDF (пріоритет над текстом) ──────────────────
            # Nemezida для ФИО+ДН присилає СПОЧАТКУ текст "Розпізнано як: ..."
            # а потім PDF. Щоб текст не перехопив відповідь — перевіряємо всі
            # нові повідомлення на наявність PDF, і тільки якщо PDF немає —
            # дивимося на текст.
            for m in new_msgs:
                if m.file:
                    result = await _try_download_pdf(m)
                    if result is not None:
                        return result

            # ── ПРОХІД 2: тільки якщо PDF не знайдено — перевіряємо текст ──────
            for m in new_msgs:
                if m.file:
                    continue  # вже перевірили вище
                text = (m.message or '').strip()
                if (text and len(text) > 20
                        and not _is_loading(text)
                        and not _is_nemezida_echo(text)):
                    return text

    return "Нічого не знайдено (таймаут 15 хв)"


def nemezida_search(query: str) -> str:
    """Глубокий поиск через Nemezida.
    Бот присылает PDF — скачивается и читается автоматически.
    Макс. ожидание: 15 мин (фаза 1: 5с×24, фаза 2: 15с×52)."""
    try:
        result = _run_tg(_tg_nemezida_query(query), timeout=960)  # 16 мин запас
        return f"[Nemezida]\n{result}"
    except Exception as e:
        return f"[Nemezida] Ошибка: {e}"


# ─── Sherlock ─────────────────────────────────────────────────────────────────

def _parse_sherlock_html_socials(html_content: str) -> str:
    """
    Витягує тільки соцмережі з HTML-звіту sherlock-report.at.
    Email не чіпає — він береться окремо з /txt.
    Повертає рядки: "Платформа: URL"
    """
    lines     = []
    seen_urls: set = set()

    # ── BeautifulSoup: всі <a href> ───────────────────────────────────────────
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            if href in seen_urls or not href.startswith('http'):
                continue
            label = _classify_social_url(href)
            if label:
                seen_urls.add(href)
                lines.append(f'{label}: {href}')
    except Exception:
        pass

    # ── Fallback: regex ───────────────────────────────────────────────────────
    if not lines:
        SOCIAL_RE = [
            ('ВКонтакте',     r'https?://(?:www\.)?vk\.com/[^\s"\'<>&]+'),
            ('Telegram',      r'https?://(?:www\.)?t\.me/[^\s"\'<>&]+'),
            ('Instagram',     r'https?://(?:www\.)?instagram\.com/[^\s"\'<>&]+'),
            ('Facebook',      r'https?://(?:www\.)?facebook\.com/[^\s"\'<>&]+'),
            ('Twitter/X',     r'https?://(?:www\.)?(?:twitter|x)\.com/[^\s"\'<>&]+'),
            ('TikTok',        r'https?://(?:www\.)?tiktok\.com/@[^\s"\'<>&]+'),
            ('YouTube',       r'https?://(?:www\.)?youtube\.com/[^\s"\'<>&]+'),
            ('Одноклассники', r'https?://(?:www\.)?ok\.ru/[^\s"\'<>&]+'),
            ('LinkedIn',      r'https?://(?:www\.)?linkedin\.com/in/[^\s"\'<>&]+'),
        ]
        for platform, pattern in SOCIAL_RE:
            for url in re.findall(pattern, html_content):
                url = url.rstrip('.,;)\'">')
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    lines.append(f'{platform}: {url}')

    return '\n'.join(lines)


def _classify_social_url(url: str) -> str:
    """Визначає назву платформи за URL. Повертає порожній рядок якщо не соцмережа."""
    u = url.lower()
    if 'vk.com' in u:           return 'ВКонтакте'
    if 't.me/' in u:             return 'Telegram'
    if 'instagram.com' in u:     return 'Instagram'
    if 'facebook.com' in u:      return 'Facebook'
    if 'twitter.com' in u or 'x.com' in u: return 'Twitter/X'
    if 'tiktok.com' in u:        return 'TikTok'
    if 'youtube.com' in u:       return 'YouTube'
    if 'ok.ru' in u:             return 'Одноклассники'
    if 'linkedin.com' in u:      return 'LinkedIn'
    if 'github.com' in u:        return 'GitHub'
    if 'pinterest.com' in u:     return 'Pinterest'
    if 'tumblr.com' in u:        return 'Tumblr'
    if 'twitch.tv' in u:         return 'Twitch'
    if 'reddit.com' in u:        return 'Reddit'
    if 'steam' in u:             return 'Steam'
    return ''


async def _tg_sherlock_query(query: str) -> str:
    """
    Повний цикл роботи з @Officekyrashu_bot:
    1. Надсилаємо номер телефону
    2. Чекаємо повідомлення з кнопкою "Открыть полный отчет"
    3. Зчитуємо URL з кнопки (KeyboardButtonUrl) — без кліку в Telegram
    4. Завантажуємо звіт через requests (HTML + /txt для email)
    5. Повертаємо список соцмереж + email

    Кнопка — це KeyboardButtonUrl, вона відкриває сайт sherlock-report.at.
    Сторінка доступна без авторизації і безкоштовно.
    """
    import requests as _requests

    bot = _BOT_SHERLOCK

    msgs_before = await _tg_client.get_messages(bot, limit=1)
    last_id = msgs_before[0].id if msgs_before else 0

    await _tg_client.send_message(bot, query)

    # ── Чекаємо відповідь з кнопкою (max 2 хв) ───────────────────────────────
    report_url = None
    for _ in range(24):
        await asyncio.sleep(5)
        msgs = await _tg_client.get_messages(bot, limit=10)
        for m in sorted(msgs, key=lambda x: x.id):
            if m.id <= last_id:
                continue
            if not (m.reply_markup and hasattr(m.reply_markup, 'rows')):
                continue
            for row in m.reply_markup.rows:
                for btn in row.buttons:
                    btn_text = btn.text.lower() if hasattr(btn, 'text') else ''
                    if (hasattr(btn, 'url') and btn.url and
                            any(w in btn_text for w in ('отчет', 'звіт', 'report', 'відкрити', 'открыть'))):
                        report_url = btn.url
                        break
                if report_url:
                    break
            if report_url:
                break
        if report_url:
            break

    if not report_url:
        return ''

    # ── Завантажуємо HTML-звіт і /txt через requests ─────────────────────────
    headers = {'User-Agent': 'Mozilla/5.0'}
    lines   = []
    seen_emails: set = set()

    # Соцмережі — з HTML
    try:
        r = _requests.get(report_url, timeout=30, headers=headers)
        if r.status_code == 200:
            socials = _parse_sherlock_html_socials(r.text)
            if socials:
                lines.append(socials)
    except Exception:
        pass

    # Email — з /txt (там чистіший список)
    try:
        r_txt = _requests.get(report_url + '/txt', timeout=30, headers=headers)
        if r_txt.status_code == 200:
            emails_raw = re.findall(
                r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+',
                r_txt.text,
            )
            skip_ext = ('.png', '.jpg', '.svg', '.gif', '.css', '.js', '.woff')
            seen_lower: set = set()
            emails = []
            for e in emails_raw:
                el = e.lower()
                if (el not in seen_lower
                        and el not in seen_emails
                        and not any(el.endswith(x) for x in skip_ext)
                        and '.' in e.split('@')[-1]
                        and len(e) < 80):
                    seen_lower.add(el)
                    emails.append(e.lower())  # нормалізуємо до нижнього регістру
            if emails:
                seen_emails.update(emails)
                lines.append(f'E-mail (з Sherlock): {", ".join(emails)}')
    except Exception:
        pass

    if report_url:
        lines.insert(0, f"Повний звіт: {report_url}")
    return '\n'.join(lines)


def sherlock_search(query: str) -> str:
    """
    Пошук за номером телефону через Sherlock-бота.
    Клікає кнопку "Открыть полный отчет", завантажує HTML,
    витягує соцмережі + email.
    query: номер телефону (напр. +380966616192) або username.
    """
    try:
        result = _run_tg(_tg_sherlock_query(query), timeout=360)
        if not result:
            return f"[Sherlock] Нічого не знайдено для: {query}"
        return f"[Sherlock — {query}]\n{result}"
    except Exception as e:
        return f"[Sherlock] Ошибка: {e}"


# ─── FanStatistik ─────────────────────────────────────────────────────────────
def fanstatistik_search(query: str) -> str:
    """
    История Telegram usernames и ID через @funstatistiik_bot.
    Принимает Telegram numeric ID или @username.
    Читает только первый (бесплатный) ответ — ID + все usernames.
    Никакие кнопки не кликает (платные).
    """
    async def _run():
        bot = _BOT_FANSTATISTIK

        msgs_before = await _tg_client.get_messages(bot, limit=1)
        last_id = msgs_before[0].id if msgs_before else 0

        # Числовий ID — відправляємо як є; username — завжди з @
        q = str(query).strip()
        if not q.startswith('@') and not q.lstrip('-').isdigit():
            q = '@' + q
        await _tg_client.send_message(bot, q)

        # Ждём ответ бота с реальными данными
        # Бот использует Unicode-имитаторы ("ΙＤ:" вместо "ID:", "ᴜѕerηαmеs:" вместо "usernames:")
        # поэтому детектируем по длине (реальный ответ > 80 символов) и наличию "@"
        response_msg = None
        for _ in range(24):
            await asyncio.sleep(3)
            msgs = await _tg_client.get_messages(bot, limit=10)
            new = sorted([m for m in msgs if m.id > last_id], key=lambda m: m.id)
            for m in new:
                text = (m.message or '').strip()
                # Реальный ответ длинный и содержит @ (ники пользователей)
                if len(text) > 80 and '@' in text:
                    response_msg = m
                    break
            if response_msg:
                break

        if not response_msg:
            return "Нічого не знайдено (таймаут)"

        # Читаємо тільки перший безкоштовний відповідь — НЕ клікаємо жодних кнопок
        full_text = (response_msg.message or '').strip()

        # Витягуємо рядки починаючи з ID: та usernames:
        lines = full_text.split('\n')
        result_lines = []
        capture = False
        for line in lines:
            low = line.lower()
            if 'id:' in low or 'username' in low:
                capture = True
            if capture:
                # Зупиняємось перед "Имена:" якщо там немає нічого корисного
                result_lines.append(line)

        parsed = '\n'.join(result_lines).strip() if result_lines else full_text
        return parsed

    try:
        result = _run_tg(_run(), timeout=120)
        return f"[FanStatistik — Telegram-история]\n{result}"
    except Exception as e:
        return f"[FanStatistik] Ошибка: {e}"


# ─── Реестр инструментов для Claude ──────────────────────────────────────────
TOOL_DEFINITIONS = [
    {
        "name": "thundertrace_search",
        "description": "Поиск username по 350+ платформам одновременно.",
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "Username для поиска"},
            },
            "required": ["username"],
        },
    },
    {
        "name": "nemezida_search",
        "description": "Глубокий OSINT через Nemezida. Принимает: ИПН (10 цифр), номер телефона (+380XXXXXXXXX или 0XXXXXXXXX), 'Прізвище Ім'я По батькові ДД.ММ.РРРР' для поиска по ФИО+дата.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "ИПН (10 цифр) ИЛИ телефон (+380XXXXXXXXX) ИЛИ 'ПІБ ДД.ММ.РРРР'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "sherlock_search",
        "description": (
            "Пошук соціальних мереж за номером телефону. "
            "Натискає кнопку 'Открыть полный отчет', завантажує HTML-файл, "
            "витягує знайдені соцмережі (ВКонтакте, Telegram, Instagram, TikTok і т.д.) "
            "та email-адреси. "
            "Використовувати для КОЖНОГО телефону кандидата та його родичів."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Номер телефону (напр. +380966616192)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fanstatistik_search",
        "description": (
            "История Telegram аккаунта: все usernames и имена которые использовал человек. "
            "Принимает Telegram numeric ID или @username."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Telegram numeric ID или @username"},
            },
            "required": ["query"],
        },
    },
]

TOOL_FUNCTIONS = {
    "thundertrace_search": lambda inp: thundertrace_search(inp["username"]),
    "nemezida_search":     lambda inp: nemezida_search(inp["query"]),
    "sherlock_search":     lambda inp: sherlock_search(inp["query"]),
    "fanstatistik_search": lambda inp: fanstatistik_search(inp["query"]),
}
