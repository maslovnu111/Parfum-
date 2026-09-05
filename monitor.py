#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Douglas price & promo monitor
─────────────────────────────
Стежить за ціною та акціями конкретного товару на douglas.de
і надсилає сповіщення в Telegram.

Сповіщення надходить, якщо:
  • ціна впала;
  • на сторінці з'явилася перекреслена (стара) ціна — тобто знижка;
  • з'явився новий промокод (Code: XXXX);
  • з'явився новий акційний рядок (%, Rabatt, Gutschein, GRATIS, statt ...);
  • товар зник/повернувся в наявність;
  • парсер зламався 3 рази поспіль (щоб бот не мовчав непомітно).

Підвищення ціни зберігається в стан, але сповіщення НЕ надсилає.
"""

import hashlib
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # bs4 не обов'язковий, є запасний варіант на regex
    BeautifulSoup = None

try:
    import cloudscraper
except ImportError:  # cloudscraper теж не обов'язковий
    cloudscraper = None

try:
    from curl_cffi import requests as cffi_requests  # імітує TLS-відбиток браузера
except ImportError:
    cffi_requests = None


# ══════════════════ НАЛАШТУВАННЯ ══════════════════

PRODUCT_URL = "https://www.douglas.de/de/p/5011160013"
PRODUCT_NAME = "Essential Parfums Bois Imperial EdP"

# Який об'єм відстежуємо. Щоб перемкнутися на 150 мл — заміни на "150 ml".
VARIANT = "100 ml"

STATE_FILE = "state.json"
KYIV_TZ = timezone(timedelta(hours=3))  # влітку UTC+3

# Скільки символів ПЕРЕД згадкою об'єму шукаємо ціну.
PRICE_WINDOW = 90

# Розумні межі ціни, щоб не зловити випадкове число зі сторінки.
MIN_SANE_PRICE = 5.0
MAX_SANE_PRICE = 1000.0

# Рядки, які ігноруємо в акціях (це постійні пункти меню, а не акції).
PROMO_IGNORE = {
    "sale", "angebote", "preis-tipp", "geschenke", "goodies",
    "angebote menü öffnen", "preis-tipp menü öffnen", "geschenke menü öffnen",
}

PROMO_KEYWORDS = [
    "%", "rabatt", "gutschein", "gratis", "statt ", "sale",
    "aktion", "reduziert", "spare", "sparen", "code:", "angebot",
]

# ══════════════════ СЕКРЕТИ / ЗМІННІ ОТОЧЕННЯ ══════════════════

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TARGET_PRICE = os.environ.get("TARGET_PRICE", "").strip()  # необов'язково
FORCE_NOTIFY = os.environ.get("FORCE_NOTIFY", "").strip().lower() in ("1", "true", "yes")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.douglas.de/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}


# ══════════════════ ДОПОМІЖНЕ ══════════════════

def log(msg):
    print(f"[{datetime.now(KYIV_TZ):%H:%M:%S}] {msg}", flush=True)


def should_alert_on_failure(fail_count: int) -> bool:
    """
    Коли саме сповіщати про технічний збій.
    1-ша і 3-тя невдала спроба поспіль — щоб одразу було видно проблему
    (особливо важливо під час першого налаштування). Далі — раз на 9 спроб,
    щоб не спамити, якщо блокування розтягнеться на дні.
    """
    return fail_count in (1, 3) or fail_count % 9 == 0


def to_float(german_price: str):
    """'1.234,56' -> 1234.56"""
    try:
        return float(german_price.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def fmt(value):
    """94.0 -> '94,00 €'"""
    if value is None:
        return "—"
    s = f"{value:,.2f}"                      # 1,234.56
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    return f"{s} €"


# ══════════════════ ЗАВАНТАЖЕННЯ СТОРІНКИ ══════════════════

def fetch_curl_cffi():
    """
    Запити з реальним TLS/HTTP2-відбитком Chrome. Anti-bot системи (DataDome,
    PerimeterX, Akamai) насамперед дивляться саме на TLS-fingerprint —
    у звичайного `requests` він показує "це не браузер" ще до заголовків.
    Не дає стовідсоткової гарантії (репутація самої IP-адреси теж важлива),
    але це найдешевший спосіб суттєво підняти шанс пройти перевірку.
    """
    if cffi_requests is None:
        raise RuntimeError("curl_cffi не встановлено")
    r = cffi_requests.get(
        PRODUCT_URL,
        headers=BROWSER_HEADERS,
        impersonate="chrome124",
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def fetch_direct():
    r = requests.get(PRODUCT_URL, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_cloudscraper():
    if cloudscraper is None:
        raise RuntimeError("cloudscraper не встановлено")
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    r = scraper.get(PRODUCT_URL, timeout=45)
    r.raise_for_status()
    return r.text


def fetch_jina():
    """Запасний варіант: текстовий проксі r.jina.ai рендерить сторінку за нас."""
    url = "https://r.jina.ai/" + PRODUCT_URL
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "text/plain"}, timeout=90)
    r.raise_for_status()
    return r.text


def get_page():
    """Пробує способи по черзі. Повертає (текст_сторінки, назва_способу)."""
    attempts = [
        ("curl_cffi (Chrome-фінгерпринт)", fetch_curl_cffi),
        ("прямий запит", fetch_direct),
        ("cloudscraper", fetch_cloudscraper),
        ("r.jina.ai", fetch_jina),
    ]
    errors = []
    for name, fn in attempts:
        try:
            raw = fn()
            if raw and len(raw) > 500:
                log(f"OK: {name} ({len(raw)} символів)")
                return raw, name
            errors.append(f"{name}: відповідь замала ({len(raw or '')})")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {e}")
            log(f"FAIL: {name} — {e}")
    raise RuntimeError(" | ".join(errors))


def to_text(raw: str) -> str:
    """HTML або markdown -> чистий текст із збереженням порядку блоків."""
    looks_like_html = bool(re.search(r"(?i)<(!doctype|html|div|body)\b", raw[:5000]))
    if looks_like_html:
        if BeautifulSoup is not None:
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            text = soup.get_text("\n")
        else:
            text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
            text = re.sub(r"(?s)<[^>]+>", "\n", text)
    else:
        text = raw
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ ]*\n+", "\n", text)
    return text.strip()


# ══════════════════ ПАРСИНГ ══════════════════

PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")
GRUNDPREIS_RE = re.compile(r"^\s*/\s*1\s*[lm]")  # '940,00 € / 1 l' — ціна за літр


def parse_prices(text: str, variant: str):
    """
    Знаходить ціни, що стоять безпосередньо ПЕРЕД згадкою об'єму.
    Так відсікаються ціна за літр і ціна сусіднього об'єму.
    Повертає відсортований список унікальних цін (від меншої).
    """
    variant_pattern = re.compile(
        r"\s*".join(re.escape(part) for part in variant.split()), re.I
    )
    variant_positions = [m.start() for m in variant_pattern.finditer(text)]
    if not variant_positions:
        return []

    # Усі згадки будь-якого об'єму ("100 ml", "150 ml", "50 ml"...) —
    # потрібні, щоб не залізти в блок сусіднього об'єму.
    size_ends = [m.end() for m in re.finditer(r"\d{1,4}\s*ml\b", text, re.I)]

    # Для кожної згадки нашого об'єму рахуємо, з якого місця дозволено шукати ціну.
    windows = []
    for pos in variant_positions:
        previous_size_end = max((e for e in size_ends if e <= pos), default=0)
        windows.append((max(previous_size_end, pos - PRICE_WINDOW), pos))

    found = set()
    for m in PRICE_RE.finditer(text):
        tail = text[m.end():m.end() + 12]
        if GRUNDPREIS_RE.match(tail):
            continue
        value = to_float(m.group(1))
        if value is None or not (MIN_SANE_PRICE <= value <= MAX_SANE_PRICE):
            continue
        for low, high in windows:
            if low <= m.start() and m.end() <= high:
                found.add(round(value, 2))
                break
    return sorted(found)


def parse_availability(text: str):
    low = text.lower()
    if re.search(r"online:\s*auf lager|auf lager", low):
        return "в наявності"
    if re.search(r"ausverkauft|nicht verfügbar|nicht auf lager|vergriffen", low):
        return "немає"
    return "невідомо"


CODE_RE = re.compile(r"Code[:\s]{1,3}([A-Z][A-Z0-9]{2,19})")


def parse_promos(text: str):
    """Повертає (список_промокодів, список_акційних_рядків)."""
    codes = sorted(set(CODE_RE.findall(text)))

    lines = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not (8 <= len(line) <= 200):
            continue
        low = line.lower()
        if low in PROMO_IGNORE:
            continue
        if any(kw in low for kw in PROMO_KEYWORDS):
            lines.append(re.sub(r"\s+", " ", line))
    # прибираємо дублікати, зберігаючи порядок, максимум 40 рядків
    seen, uniq = set(), []
    for line in lines:
        key = line.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(line)
    return codes, uniq[:40]


# ══════════════════ СТАН ══════════════════

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Не вдалося прочитати {STATE_FILE}: {e}")
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    log(f"Стан збережено в {STATE_FILE}")


# ══════════════════ TELEGRAM ══════════════════

def send_telegram(html_text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        log("TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID не задані — повідомлення не надіслано.")
        print("─" * 50)
        print(re.sub(r"<[^>]+>", "", html_text))
        print("─" * 50)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            log(f"Telegram відповів {r.status_code}: {r.text[:300]}")
            return False
        log("Повідомлення надіслано в Telegram.")
        return True
    except Exception as e:
        log(f"Помилка надсилання в Telegram: {e}")
        return False


# ══════════════════ ОСНОВНА ЛОГІКА ══════════════════

def build_message(header, price, old_price, prev_price, min_price,
                  availability, new_codes, new_promos, all_codes):
    parts = [header, ""]
    parts.append(f"<b>{PRODUCT_NAME}</b> · {VARIANT}")

    if prev_price is not None and price is not None and price != prev_price:
        diff = price - prev_price
        pct = diff / prev_price * 100 if prev_price else 0
        parts.append(f"Було: {fmt(prev_price)} → <b>зараз: {fmt(price)}</b> "
                     f"({pct:+.1f}%)")
    else:
        parts.append(f"Ціна: <b>{fmt(price)}</b>")

    if old_price and price and old_price > price:
        parts.append(f"На сторінці перекреслено: <s>{fmt(old_price)}</s> "
                     f"— знижка {(old_price - price) / old_price * 100:.0f}%")

    if min_price is not None:
        parts.append(f"Мінімум за весь час спостереження: {fmt(min_price)}")

    parts.append(f"Наявність: {availability}")

    if TARGET_PRICE:
        try:
            target = float(TARGET_PRICE.replace(",", "."))
            if price is not None and price <= target:
                parts.append(f"🎯 Це нижче твоєї цільової ціни {fmt(target)}")
        except ValueError:
            pass

    if new_codes:
        parts.append("")
        parts.append("🎟 <b>Нові промокоди:</b> " + ", ".join(f"<code>{c}</code>" for c in new_codes))
    elif all_codes:
        parts.append("")
        parts.append("🎟 Активні коди: " + ", ".join(f"<code>{c}</code>" for c in all_codes))

    if new_promos:
        parts.append("")
        parts.append("📣 <b>Нові акційні написи:</b>")
        for line in new_promos[:8]:
            safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            parts.append(f"• {safe}")

    parts.append("")
    parts.append(f'🔗 <a href="{PRODUCT_URL}">Відкрити на Douglas</a>')
    parts.append(f"<i>{datetime.now(KYIV_TZ):%d.%m.%Y %H:%M} за Києвом</i>")
    return "\n".join(parts)


def main():
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["last_check"] = now_iso

    # ── 1. Завантаження ──
    try:
        raw, source = get_page()
    except Exception as e:
        state["fail_count"] = int(state.get("fail_count", 0)) + 1
        state["last_error"] = str(e)[:500]
        save_state(state)
        log(f"Усі способи завантаження провалилися ({state['fail_count']} поспіль)")
        if should_alert_on_failure(state["fail_count"]):
            send_telegram(
                "⚠️ <b>Бот не може відкрити сторінку Douglas</b>\n\n"
                f"Невдалих спроб поспіль: {state['fail_count']}\n"
                f"<code>{str(e)[:300]}</code>\n\n"
                "Схоже на анти-бот захист сайту (типово для великих "
                "магазинів) — можлива блокування на рівні IP-адреси "
                "GitHub Actions, а не помилка в коді.\n\n"
                f'🔗 <a href="{PRODUCT_URL}">Перевірити вручну</a>'
            )
        return

    text = to_text(raw)

    # ── 2. Парсинг ──
    prices = parse_prices(text, VARIANT)
    availability = parse_availability(text)
    codes, promo_lines = parse_promos(text)

    if not prices:
        state["fail_count"] = int(state.get("fail_count", 0)) + 1
        state["last_error"] = f"ціну для «{VARIANT}» не знайдено (спосіб: {source})"
        save_state(state)
        log(state["last_error"])
        if should_alert_on_failure(state["fail_count"]):
            send_telegram(
                "⚠️ <b>Бот не знаходить ціну на сторінці</b>\n\n"
                f"Схоже, Douglas змінив верстку. Спроб поспіль: {state['fail_count']}\n"
                f'🔗 <a href="{PRODUCT_URL}">Перевірити вручну</a>'
            )
        return

    price = prices[0]                      # найменша поруч з об'ємом = актуальна
    old_price = prices[-1] if len(prices) > 1 else None
    state["fail_count"] = 0
    state.pop("last_error", None)

    prev_price = state.get("price")
    prev_old = state.get("old_price")
    prev_codes = set(state.get("promo_codes", []))
    prev_promos = set(x.lower() for x in state.get("promo_lines", []))
    prev_avail = state.get("availability")
    min_price = state.get("min_price")
    first_run = prev_price is None

    new_codes = sorted(set(codes) - prev_codes)
    new_promos = [line for line in promo_lines if line.lower() not in prev_promos]

    log(f"Ціна: {price} | стара: {old_price} | наявність: {availability} "
        f"| кодів: {len(codes)} | нових акцій: {len(new_promos)}")

    # ── 3. Що вважаємо приводом для сповіщення ──
    reasons = []
    if not first_run:
        if prev_price is not None and price < prev_price - 0.005:
            reasons.append("price_drop")
        if old_price and (not prev_old or old_price != prev_old) and old_price > price:
            reasons.append("discount")
        if new_codes:
            reasons.append("codes")
        if new_promos:
            reasons.append("promos")
        if availability != prev_avail and availability in ("в наявності", "немає"):
            reasons.append("stock")

    # ── 4. Оновлюємо стан ──
    state["price"] = price
    state["old_price"] = old_price
    state["availability"] = availability
    state["promo_codes"] = codes
    state["promo_lines"] = promo_lines
    state["source"] = source
    state["min_price"] = price if min_price is None else min(min_price, price)
    if not first_run and prev_price is not None and price != prev_price:
        history = state.get("history", [])
        history.append({"t": now_iso, "price": price})
        state["history"] = history[-60:]
    save_state(state)

    # ── 5. Надсилаємо ──
    if first_run:
        header = "✅ <b>Бот запущено</b> — стежу за ціною та акціями"
    elif FORCE_NOTIFY and not reasons:
        header = "ℹ️ <b>Перевірка вручну</b> — змін немає"
    elif "price_drop" in reasons or "discount" in reasons:
        header = "🔻 <b>Ціна впала!</b>"
    elif "codes" in reasons or "promos" in reasons:
        header = "🎉 <b>Нова акція на Douglas</b>"
    elif "stock" in reasons:
        header = f"📦 <b>Змінилася наявність: {availability}</b>"
    else:
        log("Змін немає — сповіщення не надсилаю.")
        return

    send_telegram(build_message(
        header=header,
        price=price,
        old_price=old_price,
        prev_price=None if first_run else prev_price,
        min_price=state["min_price"],
        availability=availability,
        new_codes=new_codes,
        new_promos=new_promos,
        all_codes=codes,
    ))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        # виходимо з кодом 0, щоб GitHub не спамив листами про червоні білди
        sys.exit(0)
