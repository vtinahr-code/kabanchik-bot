from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

# Новий state-файл, щоб не тягнути помилки старих версій.
STATE_FILE = Path(os.getenv("STATE_FILE_V7", "/data/state_v7.json"))

# Починаємо з відомого тобі активного замовлення.
START_TASK_ID = int(os.getenv("START_TASK_ID_V7", "4955308"))

# Скільки ID максимум перевіряємо за один цикл.
MAX_PROBES_PER_CYCLE = int(os.getenv("MAX_PROBES_PER_CYCLE", "500"))

# Коли після останнього знайденого завдання бачимо стільки порожніх ID —
# вважаємо, що наздогнали поточний кінець стрічки і чекаємо наступний цикл.
MAX_CONSECUTIVE_MISSES = int(os.getenv("MAX_CONSECUTIVE_MISSES", "80"))

HTTP_TIMEOUT = 12
PAGE_TIMEOUT_MS = 30000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5",
}

# Бухгалтерські/суміжні ключові слова.
KEYWORDS = [
    x.strip().lower() for x in os.getenv(
        "KEYWORDS_V7",
        "бухгалтер,бухгалтерія,бухгалтерські,фоп,тов,пдв,єсв,звітність,"
        "декларація,податкова,зарплата,кадри,1с,bas,m.e.doc,медок,"
        "пенсійний фонд,пфу,декрет,декретна,допомога,"
        "інвойс,invoice,commercial invoice,рахунок-фактура,рахунок фактура,"
        "зед,експорт,імпорт,первинні документи,первинка,"
        "відновлення обліку,ліквідаційна звітність"
    ).split(",") if x.strip()
]

ACTIVE_MARKERS = (
    "очікує фахівця",
    "ожидает специалиста",
)

ACTION_MARKERS = (
    "виконати",
    "відгукнутися",
    "відгукнутись",
    "подати пропозицію",
    "додати пропозицію",
    "запропонувати ціну",
    "выполнить",
    "откликнуться",
)

CLOSED_MARKERS = (
    "закрито замовником",
    "закрито автоматично",
    "скасовано замовником",
    "замовлення закрито",
    "завдання закрито",
    "заказ закрыт",
    "замовлення скасовано",
    "заказ отменен",
    "виконавець обраний",
    "исполнитель выбран",
    "прострочено",
)

session = requests.Session()
session.headers.update(HEADERS)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def telegram_send(text: str) -> None:
    r = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "last_valid_id": int(data.get("last_valid_id", START_TASK_ID - 1)),
                "seen": set(str(x) for x in data.get("seen", [])),
            }
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[STATE READ ERROR] {exc}", flush=True)

    return {
        "last_valid_id": START_TASK_ID - 1,
        "seen": set(),
    }


def save_state(last_valid_id: int, seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Щоб файл не ріс безмежно, тримаємо останні 5000 ID.
    sorted_seen = sorted((int(x) for x in seen if str(x).isdigit()))
    sorted_seen = sorted_seen[-5000:]

    STATE_FILE.write_text(
        json.dumps(
            {
                "last_valid_id": last_valid_id,
                "seen": [str(x) for x in sorted_seen],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def relevant(text: str) -> bool:
    low = text.lower()
    return any(keyword in low for keyword in KEYWORDS)


def numeric_task_url(task_id: int) -> str:
    return f"https://kabanchik.ua/ua/task/{task_id}"


def probe_task(task_id: int) -> tuple[bool, str | None]:
    """
    Перевіряємо існування ID напряму, без списку /rabota/.
    Якщо завдання існує, Kabanchik зазвичай повертає 200 або редірект на URL зі slug.
    """
    url = numeric_task_url(task_id)

    try:
        r = session.get(
            url,
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        print(f"[PROBE ERROR] {task_id}: {exc}", flush=True)
        return False, None

    if r.status_code in (404, 410):
        return False, None

    if r.status_code >= 500:
        print(f"[PROBE SERVER] {task_id}: HTTP {r.status_code}", flush=True)
        return False, None

    if r.status_code in (401, 403, 429):
        # Не вважаємо ID відсутнім: сайт міг обмежити HTTP-запит.
        # Playwright спробує відкрити його напряму.
        return True, url

    if r.status_code < 400:
        final_url = r.url
        # Якщо нас перекинуло на сторінку логіну/головну — це не task.
        path = urlsplit(final_url).path
        if "/task/" not in path:
            return False, None
        return True, final_url

    return False, None


def classify_task(page, task_id: int, url: str):
    try:
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        print(f"[PAGE TIMEOUT] {task_id}", flush=True)
        return "RETRY", None

    page.wait_for_timeout(1500)

    if response is not None and response.status in (404, 410):
        return "MISSING", None

    try:
        body = clean(page.locator("body").inner_text(timeout=10000))
    except Exception as exc:
        print(f"[BODY ERROR] {task_id}: {exc}", flush=True)
        return "RETRY", None

    low = body.lower()

    # Якщо це реально не сторінка завдання.
    if not body or ("сторінку не знайдено" in low) or ("page not found" in low):
        return "MISSING", None

    # Активність підтверджуємо статусом або реальною кнопкою.
    active_reason = next((m for m in ACTIVE_MARKERS if m in low), None)

    if not active_reason:
        try:
            action_texts = [
                clean(x).lower()
                for x in page.locator("a, button").all_inner_texts()
            ]
        except Exception:
            action_texts = []

        active_reason = next(
            (
                marker
                for text in action_texts
                for marker in ACTION_MARKERS
                if marker in text
            ),
            None,
        )

    if active_reason:
        if not relevant(body):
            print(f"[NOT_RELEVANT] {task_id}", flush=True)
            return "NOT_RELEVANT", None

        title = ""
        try:
            if page.locator("h1").count():
                title = clean(page.locator("h1").first.inner_text())
        except Exception:
            pass

        if not title:
            try:
                title = clean(page.title())
            except Exception:
                title = ""

        if not title:
            title = f"Замовлення №{task_id}"

        budget_match = re.search(
            r"(?<!\d)(\d[\d\s\u00a0]{0,9})(?:[.,]\d{1,2})?\s*(грн|₴)",
            body,
            re.I,
        )
        budget = clean(budget_match.group(0)) if budget_match else "не вказано"

        final_url = page.url
        print(f"[ACTIVE] {task_id}: {title}", flush=True)

        return "ACTIVE", {
            "id": task_id,
            "title": title[:300],
            "budget": budget,
            "url": final_url,
        }

    closed_reason = next((m for m in CLOSED_MARKERS if m in low), None)
    if closed_reason:
        print(f"[CLOSED] {task_id}: {closed_reason}", flush=True)
        return "CLOSED", None

    print(f"[UNKNOWN] {task_id}: status/action not found", flush=True)
    return "UNKNOWN", None


def cycle(browser) -> None:
    state = load_state()
    last_valid_id = state["last_valid_id"]
    seen = state["seen"]

    start_id = last_valid_id + 1
    probe_id = start_id

    consecutive_misses = 0
    probes = 0
    found_valid = 0
    sent = 0
    closed = 0
    irrelevant = 0
    unknown = 0
    retry = 0

    page = browser.new_page()

    try:
        while (
            probes < MAX_PROBES_PER_CYCLE
            and consecutive_misses < MAX_CONSECUTIVE_MISSES
        ):
            probes += 1

            exists, resolved_url = probe_task(probe_id)

            if not exists:
                consecutive_misses += 1
                probe_id += 1
                continue

            consecutive_misses = 0
            found_valid += 1

            # Знайдений реальний ID стає новою "верхньою межею".
            if probe_id > last_valid_id:
                last_valid_id = probe_id

            tid = str(probe_id)

            if tid not in seen:
                status, item = classify_task(
                    page,
                    probe_id,
                    resolved_url or numeric_task_url(probe_id),
                )

                if status == "ACTIVE" and item:
                    telegram_send(
                        "🧾 Нове АКТИВНЕ замовлення\n\n"
                        f"📌 {item['title']}\n"
                        f"💰 Бюджет: {item['budget']}\n"
                        f"🔗 {item['url']}"
                    )
                    seen.add(tid)
                    sent += 1

                elif status == "CLOSED":
                    seen.add(tid)
                    closed += 1

                elif status == "NOT_RELEVANT":
                    seen.add(tid)
                    irrelevant += 1

                elif status == "MISSING":
                    # HTTP-проба могла дати хибнопозитивний результат.
                    pass

                elif status == "UNKNOWN":
                    # Не запам'ятовуємо: наступного циклу перевіримо знову.
                    unknown += 1

                elif status == "RETRY":
                    retry += 1

            probe_id += 1

    finally:
        page.close()

    save_state(last_valid_id, seen)

    print(
        f"[SUMMARY] start={start_id} last_valid={last_valid_id} "
        f"probes={probes} valid={found_valid} sent={sent} "
        f"closed={closed} irrelevant={irrelevant} "
        f"unknown={unknown} retry={retry} misses={consecutive_misses}",
        flush=True,
    )


def main() -> None:
    first_start = not STATE_FILE.exists()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        try:
            if first_start:
                telegram_send(
                    "✅ Kabanchik monitor v7 запущено. "
                    "Тепер бот НЕ залежить від сторінки списку замовлень. "
                    "Він перевіряє нові ID завдань напряму, тому охоплює всі міста України."
                )

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
