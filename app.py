from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

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


def telegram_send(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
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
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return set()
    except Exception:
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


def fetch_tasks(page_url: str) -> list[dict]:
    response = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items: dict[str, dict] = {}

    for anchor in soup.select('a[href*="/task/"]'):
        href = anchor.get("href")
        if not href:
            continue

        task_url = urljoin(page_url, href).split("#")[0]
        title = clean(anchor.get_text(" ", strip=True))
        parent = anchor.find_parent(["article", "li", "div"])
        context = clean(parent.get_text(" ", strip=True) if parent else title)

        if not title:
            continue
        if not relevant(f"{title} {context}"):
            continue

        budget_match = re.search(r"(\d[\d\s]{1,8})\s*(грн|₴)", context, re.I)
        budget = clean(budget_match.group(0)) if budget_match else "не вказано"

        task_id = hashlib.sha256(task_url.encode("utf-8")).hexdigest()
        items[task_id] = {
            "id": task_id,
            "title": title[:300],
            "url": task_url,
            "budget": budget,
        }

    return list(items.values())


def cycle() -> None:
    seen = load_seen()
    first_run = not STATE_FILE.exists()
    current: set[str] = set()
    fresh: list[dict] = []

    for url in TARGET_URLS:
        try:
            for item in fetch_tasks(url):
                current.add(item["id"])
                if item["id"] not in seen:
                    fresh.append(item)
        except Exception as exc:
            print(f"Помилка перевірки {url}: {exc}", flush=True)

    if first_run:
        seen.update(current)
        save_seen(seen)
        telegram_send(
            "✅ Моніторинг Kabanchik запущено. Поточні оголошення запам’ятано. "
            "Далі надсилатиму лише нові бухгалтерські замовлення."
        )
        print(f"Перший запуск: запам'ятано {len(current)} оголошень", flush=True)
        return

    for item in fresh:
        telegram_send(
            "🧾 Нове замовлення на бухгалтерські послуги\n\n"
            f"📌 {item['title']}\n"
            f"💰 Бюджет: {item['budget']}\n"
            f"🔗 {item['url']}"
        )

    seen.update(current)
    save_seen(seen)
    print(f"Перевірено. Нових: {len(fresh)}", flush=True)


if __name__ == "__main__":
    while True:
        try:
            cycle()
        except Exception as exc:
            print(f"Критична помилка циклу: {exc}", flush=True)
        time.sleep(POLL_SECONDS)
