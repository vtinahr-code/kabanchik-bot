from __future__ import annotations

import hashlib, json, os, re, time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/seen.json"))

TARGET_URLS = [u.strip() for u in os.getenv(
    "TARGET_URLS",
    "https://kabanchik.ua/ua/kyiv/rabota/bukhhalterski-posluhy,https://kabanchik.ua/ua/kyiv/rabota/tag/bukhhalter"
).split(",") if u.strip()]

KEYWORDS = [x.strip().lower() for x in os.getenv(
    "KEYWORDS",
    "бухгалтер,бухгалтерія,бухгалтерські,фоп,тов,пдв,єсв,звітність,декларація,податкова,зарплата,кадри,1с,bas,m.e.doc,медок,відновлення обліку,ліквідаційна звітність,пенсійний фонд,пфу,декрет,декретна,допомога по вагітності"
).split(",") if x.strip()]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5",
}
TIMEOUT = 25

INACTIVE_MARKERS = (
    "закрито замовником", "закрито автоматично", "скасовано замовником",
    "прострочено",
    "очікує підтвердження призначеного фахівця",
    "замовлення закрито", "заказ закрыт",
    "замовлення виконано", "заказ выполнен",
    "виконавець обраний", "исполнитель выбран",
    "замовлення скасовано", "заказ отменен",
    "завдання закрито", "задание закрыто",
    "завдання виконано", "задание выполнено",
)

ACTIVE_MARKERS = ("очікує фахівця", "ожидает специалиста")
APPLY_MARKERS = (
    "виконати", "выполнить",
    "відгукнутися", "відгукнутись", "додати пропозицію",
    "запропонувати ціну", "запропонувати послугу", "подати пропозицію",
    "откликнуться", "добавить предложение",
)

session = requests.Session()
session.headers.update(HEADERS)

def telegram_send(text):
    r = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False},
        timeout=TIMEOUT,
    )
    r.raise_for_status()

def load_seen():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list): return set(map(str, data))
        if isinstance(data, dict): return set(map(str, data.get("seen", [])))
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"seen.json read error: {exc}", flush=True)
    return set()

def save_seen(seen):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def relevant(text):
    low = text.lower()
    return any(k in low for k in KEYWORDS)

def canonical_task_url(base, href):
    p = urlsplit(urljoin(base, href))
    return urlunsplit((p.scheme, p.netloc, re.sub(r"/+$", "", p.path), "", ""))

def task_id_from_url(url):
    m = re.search(r"/task/(?:[^/?#]*-)?(\d+)(?:/|$)", url)
    return m.group(1) if m else hashlib.sha256(url.encode()).hexdigest()

def fetch_candidate_urls(page_url):
    r = session.get(page_url, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = {}
    for a in soup.select('a[href*="/task/"]'):
        href = a.get("href")
        if not href: continue
        url = canonical_task_url(page_url, href)
        out[task_id_from_url(url)] = url
    return out

def has_apply_action(soup):
    for node in soup.find_all(["a", "button", "input"]):
        text = clean(node.get("value", "") if node.name == "input" else node.get_text(" ", strip=True)).lower()
        if any(m in text for m in APPLY_MARKERS):
            return True
    return False

def classify_task(soup, text):
    low = text.lower()

    # Спочатку підтверджуємо активність.
    # На Kabanchik активне завдання має статус "Очікує фахівця"
    # або доступну дію "Виконати".
    for m in ACTIVE_MARKERS:
        if m in low:
            return "ACTIVE", m

    if has_apply_action(soup):
        return "ACTIVE", "apply button/action"

    # Лише якщо активність не підтвердилась — перевіряємо закриті статуси.
    for m in INACTIVE_MARKERS:
        if m in low:
            return "CLOSED", m

    # Сумнівне завдання не надсилаємо і перевіримо повторно.
    return "UNKNOWN", "active status not confirmed"

def parse_task(task_id, task_url):
    r = session.get(task_url, timeout=TIMEOUT, allow_redirects=True)
    if r.status_code in (404, 410):
        print(f"[CLOSED] {task_id}: HTTP {r.status_code}", flush=True)
        return None, "CLOSED"
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = clean(soup.get_text(" ", strip=True))
    status, reason = classify_task(soup, text)
    print(f"[{status}] {task_id}: {reason} — {task_url}", flush=True)

    if status != "ACTIVE":
        return None, status

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta and meta.get("content"):
            title = clean(meta["content"])
    if not title:
        title = "Бухгалтерське замовлення"

    if not relevant(f"{title} {text}"):
        print(f"[NOT_RELEVANT] {task_id}: {title}", flush=True)
        return None, "NOT_RELEVANT"

    b = re.search(r"(?<!\d)(\d[\d\s\u00a0]{0,9})(?:[.,]\d{1,2})?\s*(грн|₴)", text, re.I)
    budget = clean(b.group(0)) if b else "не вказано"

    return {"id": task_id, "title": title[:300], "url": canonical_task_url(task_url, r.url), "budget": budget}, "ACTIVE"

def cycle():
    seen = load_seen()
    first_run = not STATE_FILE.exists()
    current = {}

    for page_url in TARGET_URLS:
        try:
            current.update(fetch_candidate_urls(page_url))
        except Exception as exc:
            print(f"List error {page_url}: {exc}", flush=True)

    if first_run:
        seen.update(current.keys())
        save_seen(seen)
        telegram_send(
            "✅ Моніторинг Kabanchik v4 запущено. Поточні актуальні оголошення запам’ятано. "
            "Далі надсилатиму лише нові підтверджено активні бухгалтерські замовлення."
        )
        return

    new_ids = [i for i in current if i not in seen]
    sent = closed = unknown = not_rel = errors = 0

    for task_id in new_ids:
        try:
            item, status = parse_task(task_id, current[task_id])
        except Exception as exc:
            print(f"[ERROR] {task_id}: {exc}", flush=True)
            errors += 1
            continue

        if status == "ACTIVE" and item:
            telegram_send(
                "🧾 Нове ПІДТВЕРДЖЕНО АКТИВНЕ замовлення на бухгалтерські послуги\n\n"
                f"📌 {item['title']}\n"
                f"💰 Бюджет: {item['budget']}\n"
                f"🔗 {item['url']}"
            )
            sent += 1
            seen.add(task_id)
        elif status == "CLOSED":
            closed += 1
            seen.add(task_id)
        elif status == "NOT_RELEVANT":
            not_rel += 1
            seen.add(task_id)
        else:
            unknown += 1
            # UNKNOWN deliberately not saved, so it will be rechecked next cycle.

    save_seen(seen)
    print(
        f"Summary: new={len(new_ids)} sent={sent} closed={closed} "
        f"not_relevant={not_rel} unknown={unknown} errors={errors}",
        flush=True,
    )

if __name__ == "__main__":
    while True:
        try:
            cycle()
        except Exception as exc:
            print(f"Critical cycle error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)
