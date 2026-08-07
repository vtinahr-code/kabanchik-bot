from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/seen.json"))

TARGET_URLS = [
    u.strip() for u in os.getenv(
        "TARGET_URLS",
        "https://kabanchik.ua/ua/business/,"
        "https://kabanchik.ua/ua/kyiv/category/bukhhalterski-posluhy"
    ).split(",") if u.strip()
]

KEYWORDS = [
    x.strip().lower() for x in os.getenv(
        "KEYWORDS",
        "бухгалтер,бухгалтерія,бухгалтерські,фоп,тов,пдв,єсв,звітність,"
        "декларація,податкова,зарплата,кадри,1с,bas,m.e.doc,медок,"
        "відновлення обліку,ліквідаційна звітність"
    ).split(",") if x.strip()
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5",
}
TIMEOUT = 25

# Якщо на сторінці є одна з цих фраз — замовлення вже не треба надсилати.
CLOSED_MARKERS = (
    "замовлення закрито",
    "заказ закрыт",
    "замовлення виконано",
    "заказ выполнен",
    "виконавець обраний",
    "исполнитель выбран",
    "замовлення скасовано",
    "заказ отменен",
    "завдання закрито",
    "задание закрыто",
    "завдання виконано",
    "задание выполнено",
    "архівне замовлення",
    "архивный заказ",
)

session = requests.Session()
session.headers.update(HEADERS)


def telegram_send(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = session.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def load_seen() -> set[str]:
    """Сумісно зі старим seen.json, де зберігався просто список ID."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data}
        if isinstance(data, dict):
            # На випадок майбутнього розширення формату.
            raw = data.get("seen", [])
            return {str(x) for x in raw}
        return set()
    except FileNotFoundError:
        return set()
    except Exception as exc:
        print(f"Не вдалося прочитати seen.json: {exc}", flush=True)
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def relevant(text: str) -> bool:
    low = text.lower()
    return any(word in low for word in KEYWORDS)


def canonical_task_url(base_url: str, href: str) -> str:
    """Прибирає query/fragment, щоб одне замовлення не ставало різними ID."""
    absolute = urljoin(base_url, href)
    parts = urlsplit(absolute)
    path = re.sub(r"/+$", "", parts.path)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def task_id_from_url(task_url: str) -> str:
    """
    Якщо в URL є числовий ID — використовуємо його.
    Інакше fallback на стабільний hash канонічного URL.
    """
    match = re.search(r"/task/(?:[^/?#]*-)?(\d+)(?:/|$)", task_url)
    if match:
        return match.group(1)
    return hashlib.sha256(task_url.encode("utf-8")).hexdigest()


def fetch_candidate_urls(page_url: str) -> dict[str, str]:
    """
    На сторінці списку беремо ВСІ посилання на /task/.
    Тут навмисно не фільтруємо за текстом — бо короткий заголовок у списку
    може не містити слова 'бухгалтер', хоча воно є всередині замовлення.
    """
    response = session.get(page_url, timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    items: dict[str, str] = {}
    for anchor in soup.select('a[href*="/task/"]'):
        href = anchor.get("href")
        if not href:
            continue

        task_url = canonical_task_url(page_url, href)
        task_id = task_id_from_url(task_url)
        items[task_id] = task_url

    return items


def parse_task(task_id: str, task_url: str) -> dict | None:
    """
    Відкриває САМЕ замовлення перед відправкою.
    Це прибирає головну помилку старої версії: закрите завдання більше
    не відправляється лише тому, що Kabanchik показав його у списку.
    """
    response = session.get(task_url, timeout=TIMEOUT, allow_redirects=True)

    # Видалене/недоступне завдання.
    if response.status_code in (404, 410):
        return None
    response.raise_for_status()

    final_url = canonical_task_url(task_url, response.url)
    soup = BeautifulSoup(response.text, "html.parser")

    # Беремо текст сторінки без script/style, щоб не ловити службові слова.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    page_text = clean(soup.get_text(" ", strip=True))
    page_low = page_text.lower()

    # Відсікаємо тільки ЯВНО закриті/скасовані/виконані замовлення.
    # Якщо кнопка змінила назву, нове активне замовлення через це не пропустимо.
    closed_reason = next((m for m in CLOSED_MARKERS if m in page_low), None)
    if closed_reason:
        print(
            f"Пропущено закрите замовлення {task_id}: {closed_reason} — {final_url}",
            flush=True,
        )
        return None

    # Заголовок краще брати зі сторінки завдання, а не з картки списку.
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = clean(h1.get_text(" ", strip=True))

    if not title:
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            title = clean(meta["content"])

    if not title:
        title = "Бухгалтерське замовлення"

    # Ключові слова перевіряємо по ВСІЙ сторінці замовлення.
    # Це важливо: старий бот перевіряв лише короткий блок у списку.
    if not relevant(f"{title} {page_text}"):
        print(f"Не бухгалтерське: {task_id} — {title}", flush=True)
        return None

    # Бюджет: спочатку шукаємо грн/₴ по сторінці.
    budget_match = re.search(
        r"(?<!\d)(\d[\d\s\u00a0]{0,9})(?:[.,]\d{1,2})?\s*(грн|₴)",
        page_text,
        re.I,
    )
    budget = clean(budget_match.group(0)) if budget_match else "не вказано"

    return {
        "id": task_id,
        "title": title[:300],
        "url": final_url,
        "budget": budget,
    }


def cycle() -> None:
    seen = load_seen()
    first_run = not STATE_FILE.exists()

    current: dict[str, str] = {}

    # 1) Збираємо кандидатів з усіх сторінок.
    for page_url in TARGET_URLS:
        try:
            current.update(fetch_candidate_urls(page_url))
        except Exception as exc:
            print(f"Помилка перевірки {page_url}: {exc}", flush=True)

    if first_run:
        # На першому запуску нічого старого не шлемо.
        seen.update(current.keys())
        save_seen(seen)
        telegram_send(
            "✅ Моніторинг Kabanchik запущено. Поточні оголошення запам’ятано. "
            "Далі надсилатиму лише нові активні бухгалтерські замовлення."
        )
        print(
            f"Перший запуск: запам'ятано {len(current)} посилань на замовлення",
            flush=True,
        )
        return

    new_ids = [task_id for task_id in current if task_id not in seen]
    sent = 0
    skipped = 0

    # 2) Кожне НОВЕ посилання відкриваємо окремо і перевіряємо статус.
    for task_id in new_ids:
        task_url = current[task_id]
        try:
            item = parse_task(task_id, task_url)
            if item is not None:
                telegram_send(
                    "🧾 Нове АКТИВНЕ замовлення на бухгалтерські послуги\n\n"
                    f"📌 {item['title']}\n"
                    f"💰 Бюджет: {item['budget']}\n"
                    f"🔗 {item['url']}"
                )
                sent += 1
            else:
                skipped += 1
        except Exception as exc:
            # ВАЖЛИВО: якщо сторінка тимчасово не відкрилась —
            # НЕ додаємо ID в seen. На наступному циклі бот спробує ще раз.
            print(
                f"Не вдалося перевірити нове замовлення {task_url}: {exc}",
                flush=True,
            )
            continue

        # Додаємо в seen тільки після успішної перевірки сторінки:
        # активне — відправили; закрите/нерелевантне — свідомо пропустили.
        seen.add(task_id)

    save_seen(seen)
    print(
        f"Перевірено. Нових кандидатів: {len(new_ids)}, "
        f"надіслано: {sent}, свідомо пропущено: {skipped}",
        flush=True,
    )


if __name__ == "__main__":
    while True:
        try:
            cycle()
        except Exception as exc:
            print(f"Критична помилка циклу: {exc}", flush=True)
        time.sleep(POLL_SECONDS)
