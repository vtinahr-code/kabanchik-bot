from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE_V5", "/data/seen_v5.json"))

TARGET_URLS = [
    u.strip() for u in os.getenv(
        "TARGET_URLS_V5",
        "https://kabanchik.ua/ua/kyiv/rabota/bukhhalterski-posluhy"
    ).split(",") if u.strip()
]

KEYWORDS = [
    x.strip().lower() for x in os.getenv(
        "KEYWORDS",
        "бухгалтер,бухгалтерія,бухгалтерські,фоп,тов,пдв,єсв,звітність,"
        "декларація,податкова,зарплата,кадри,1с,bas,m.e.doc,медок,"
        "пенсійний фонд,пфу,декрет,декретна,допомога,ліквідаційна звітність"
    ).split(",") if x.strip()
]

ACTIVE_MARKERS = ("очікує фахівця", "ожидает специалиста")
CLOSED_MARKERS = (
    "закрито замовником", "закрито автоматично", "скасовано замовником",
    "замовлення закрито", "завдання закрито", "заказ закрыт",
    "замовлення скасовано", "заказ отменен",
    "виконавець обраний", "исполнитель выбран", "прострочено",
)
ACTION_MARKERS = (
    "виконати", "відгукнутися", "відгукнутись", "подати пропозицію",
    "додати пропозицію", "запропонувати ціну", "выполнить", "откликнуться"
)

HTTP_TIMEOUT = 25
PAGE_TIMEOUT_MS = 30000

def telegram_send(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()

def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def canonical_url(base: str, href: str) -> str:
    absolute = urljoin(base, href)
    p = urlsplit(absolute)
    return urlunsplit((p.scheme, p.netloc, re.sub(r"/+$", "", p.path), "", ""))

def task_id(task_url: str) -> str:
    matches = re.findall(r"(\d{5,})", urlsplit(task_url).path)
    return matches[-1] if matches else task_url

def relevant(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in KEYWORDS)

def load_seen() -> set[str]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except FileNotFoundError:
        return set()
    except Exception as exc:
        print(f"[STATE READ ERROR] {exc}", flush=True)
        return set()

def save_seen(seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def collect_live_tasks(page, list_url: str) -> dict[str, str]:
    page.goto(list_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(3500)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.65)")
    page.wait_for_timeout(1500)

    hrefs = page.locator('a[href*="/task/"]').evaluate_all(
        "(els) => els.map(e => e.getAttribute('href')).filter(Boolean)"
    )

    result = {}
    for href in hrefs:
        url = canonical_url(list_url, href)
        if re.search(r"/task/(create|assign)(?:/|$)", url):
            continue
        tid = task_id(url)
        if re.fullmatch(r"\d{5,}", tid):
            result[tid] = url

    print(f"[LIST] {list_url}: live task links={len(result)}", flush=True)
    return result

def classify_detail(page, url: str):
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(2200)

    body = clean(page.locator("body").inner_text(timeout=10000))
    low = body.lower()

    active_reason = next((m for m in ACTIVE_MARKERS if m in low), None)

    if not active_reason:
        try:
            texts = [clean(x).lower() for x in page.locator("a, button").all_inner_texts()]
        except Exception:
            texts = []
        active_reason = next(
            (m for text in texts for m in ACTION_MARKERS if m in text),
            None,
        )

    if active_reason:
        if not relevant(body):
            print(f"[NOT_RELEVANT] {url}", flush=True)
            return "NOT_RELEVANT", None

        title = ""
        try:
            if page.locator("h1").count():
                title = clean(page.locator("h1").first.inner_text())
        except Exception:
            pass
        if not title:
            title = "Бухгалтерське замовлення"

        b = re.search(r"(?<!\d)(\d[\d\s\u00a0]{0,9})(?:[.,]\d{1,2})?\s*(грн|₴)", body, re.I)
        budget = clean(b.group(0)) if b else "не вказано"

        print(f"[ACTIVE] {url}: {active_reason}", flush=True)
        return "ACTIVE", {"title": title[:300], "budget": budget, "url": url}

    closed_reason = next((m for m in CLOSED_MARKERS if m in low), None)
    if closed_reason:
        print(f"[CLOSED] {url}: {closed_reason}", flush=True)
        return "CLOSED", None

    print(f"[UNKNOWN] {url}: active status/action not found", flush=True)
    return "UNKNOWN", None

def cycle(browser) -> None:
    seen = load_seen()
    first_run = not STATE_FILE.exists()

    list_page = browser.new_page()
    current = {}
    try:
        for list_url in TARGET_URLS:
            try:
                current.update(collect_live_tasks(list_page, list_url))
            except Exception as exc:
                print(f"[LIST ERROR] {list_url}: {exc}", flush=True)
    finally:
        list_page.close()

    if first_run:
        seen.update(current.keys())
        save_seen(seen)
        telegram_send(
            "✅ Kabanchik monitor v5 запущено. "
            "Тепер список замовлень зчитується через браузер (Playwright). "
            "Поточні замовлення запам’ятано."
        )
        print(f"[FIRST RUN] remembered={len(current)}", flush=True)
        return

    new_ids = [tid for tid in current if tid not in seen]
    print(f"[CYCLE] current={len(current)} new={len(new_ids)}", flush=True)

    detail_page = browser.new_page()
    try:
        for tid in new_ids:
            url = current[tid]
            try:
                status, item = classify_detail(detail_page, url)
            except PlaywrightTimeoutError as exc:
                print(f"[TIMEOUT] {tid}: {exc}", flush=True)
                continue
            except Exception as exc:
                print(f"[DETAIL ERROR] {tid}: {exc}", flush=True)
                continue

            if status == "ACTIVE" and item:
                telegram_send(
                    "🧾 Нове АКТИВНЕ бухгалтерське замовлення\n\n"
                    f"📌 {item['title']}\n"
                    f"💰 Бюджет: {item['budget']}\n"
                    f"🔗 {item['url']}"
                )
                seen.add(tid)
            elif status in ("CLOSED", "NOT_RELEVANT"):
                seen.add(tid)
    finally:
        detail_page.close()

    save_seen(seen)

def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            while True:
                try:
                    cycle(browser)
                except Exception as exc:
                    print(f"[CRITICAL] {exc}", flush=True)
                time.sleep(POLL_SECONDS)
        finally:
            browser.close()

if __name__ == "__main__":
    main()
