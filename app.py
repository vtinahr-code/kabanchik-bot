from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

import requests


# ============================================================
# RAILWAY / TELEGRAM
# ============================================================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

# Файл стану.
# Залишаю назву зі старого бота, щоб не ламати логіку.
STATE_FILE = Path(
    os.getenv("STATE_FILE_V7", "/data/state_v7.json")
)

# Якщо state-файлу ще немає — стартуємо звідси.
# Це значення було у твоєму попередньому коді.
START_TASK_ID = int(
    os.getenv("START_TASK_ID_V7", "4955308")
)

# Скільки ID перевіряємо вперед за один цикл.
# Старий бот зупинявся після 80 "порожніх".
# Тепер такого стопа немає.
SCAN_AHEAD = int(
    os.getenv("SCAN_AHEAD", "1200")
)

# Паралельні запити, щоб 1200 ID не перевірялися вічність.
WORKERS = int(
    os.getenv("WORKERS", "15")
)

REQUEST_TIMEOUT = int(
    os.getenv("HTTP_TIMEOUT", "12")
)

BASE_TASK_URL = "https://kabanchik.ua/ua/task/{}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}


# ============================================================
# КЛЮЧОВІ СЛОВА
# ============================================================

KEYWORDS = [
    "бухгалтер",
    "бухгалтерія",
    "бухгалтерський",
    "бухгалтерські",
    "бухоблік",
    "облік",
    "податков",
    "пдв",
    "фоп",
    "тов",
    "звітність",
    "звіт",
    "деклараці",
    "зарплат",
    "кадров",
    "1с",
    "bas",
    "медок",
    "m.e.doc",
]


# ============================================================
# LOG
# ============================================================

def log(message: str) -> None:
    print(
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "|",
        message,
        flush=True,
    )


# ============================================================
# STATE
# ============================================================

def default_state() -> dict:
    return {
        "last_scan_id": START_TASK_ID,
        "sent_ids": [],
    }


def load_state() -> dict:
    try:
        if not STATE_FILE.exists():
            log(
                f"STATE FILE NOT FOUND. "
                f"START FROM {START_TASK_ID}"
            )
            return default_state()

        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:
            state = json.load(f)

        # Підтримка старого state-файлу
        if "last_scan_id" not in state:
            old_id = (
                state.get("last_valid_id")
                or state.get("last_task_id")
                or START_TASK_ID
            )

            state["last_scan_id"] = int(old_id)

        state.setdefault("sent_ids", [])

        state["sent_ids"] = [
            int(x)
            for x in state["sent_ids"]
            if str(x).isdigit()
        ]

        return state

    except Exception as exc:
        log(f"STATE LOAD ERROR: {repr(exc)}")
        return default_state()


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        # Не зберігаємо нескінченний список.
        state["sent_ids"] = state["sent_ids"][-10000:]

        tmp = STATE_FILE.with_suffix(".tmp")

        with tmp.open(
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2,
            )

        tmp.replace(STATE_FILE)

    except Exception as exc:
        log(f"STATE SAVE ERROR: {repr(exc)}")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text: str) -> bool:
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            },
            timeout=15,
        )

        if response.ok:
            return True

        log(
            "TELEGRAM ERROR "
            f"{response.status_code}: "
            f"{response.text[:500]}"
        )

    except Exception as exc:
        log(
            f"TELEGRAM EXCEPTION: "
            f"{repr(exc)}"
        )

    return False


# ============================================================
# HTML -> TEXT
# ============================================================

def html_to_text(source: str) -> str:
    # Забираємо script/style
    source = re.sub(
        r"(?is)<script.*?>.*?</script>",
        " ",
        source,
    )

    source = re.sub(
        r"(?is)<style.*?>.*?</style>",
        " ",
        source,
    )

    # HTML-теги -> пробіли
    source = re.sub(
        r"(?s)<[^>]+>",
        " ",
        source,
    )

    source = html.unescape(source)

    source = re.sub(
        r"\s+",
        " ",
        source,
    )

    return source.strip()


def extract_title(source: str) -> str:
    # Спочатку пробуємо H1
    match = re.search(
        r"(?is)<h1[^>]*>(.*?)</h1>",
        source,
    )

    if match:
        title = html_to_text(match.group(1))

        if title:
            return title[:350]

    # Якщо H1 немає — title
    match = re.search(
        r"(?is)<title[^>]*>(.*?)</title>",
        source,
    )

    if match:
        return html_to_text(
            match.group(1)
        )[:350]

    return "Нове замовлення"


# ============================================================
# ПЕРЕВІРКА TASK ID
# ============================================================

def probe_task(task_id: int):
    url = BASE_TASK_URL.format(task_id)

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

    except requests.RequestException as exc:
        log(
            f"HTTP ERROR {task_id}: "
            f"{type(exc).__name__}"
        )
        return None

    if response.status_code != 200:
        return None

    final_url = response.url

    try:
        path = urlsplit(final_url).path.lower()
    except Exception:
        return None

    # Якщо Kabanchik перекинув нас на іншу сторінку,
    # це не вважаємо існуючим task.
    if "/task/" not in path:
        return None

    page_text = html_to_text(response.text)

    if len(page_text) < 100:
        return None

    lower_text = page_text.lower()

    not_found_markers = [
        "сторінку не знайдено",
        "сторінка не знайдена",
        "страница не найдена",
        "завдання не знайдено",
        "замовлення не знайдено",
        "page not found",
    ]

    if any(
        marker in lower_text
        for marker in not_found_markers
    ):
        return None

    title = extract_title(response.text)

    return {
        "id": task_id,
        "url": final_url,
        "title": title,
        "text": page_text,
    }


# ============================================================
# ФІЛЬТР
# ============================================================

def is_accounting_task(task: dict) -> bool:
    text = (
        task.get("title", "")
        + " "
        + task.get("text", "")
    ).lower()

    return any(
        keyword in text
        for keyword in KEYWORDS
    )


# ============================================================
# ОБРОБКА TASK
# ============================================================

def process_task(
    task: dict,
    state: dict,
) -> None:

    task_id = int(task["id"])

    if task_id in state["sent_ids"]:
        log(
            f"ALREADY SENT {task_id}"
        )
        return

    if not is_accounting_task(task):
        log(
            f"SKIP {task_id}: "
            f"not accounting"
        )
        return

    title = re.sub(
        r"\s+",
        " ",
        task.get(
            "title",
            "Нове замовлення"
        ),
    ).strip()

    log(
        f"FOUND ACCOUNTING: "
        f"{task_id} | {title}"
    )

    message = (
        "🔥 <b>Нове замовлення Kabanchik</b>\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"🆔 {task_id}\n"
        f"🔗 {html.escape(task['url'])}"
    )

    if send_telegram(message):
        state["sent_ids"].append(task_id)
        save_state(state)

        log(
            f"SENT TO TELEGRAM: "
            f"{task_id}"
        )

    else:
        log(
            f"TELEGRAM SEND FAILED: "
            f"{task_id}"
        )


# ============================================================
# SCAN
# ============================================================

def scan_cycle(state: dict) -> None:
    start_from = int(
        state.get(
            "last_scan_id",
            START_TASK_ID
        )
    )

    first_id = start_from + 1
    last_id = start_from + SCAN_AHEAD

    log(
        "-----------------------------------"
    )

    log(
        f"SCAN START: "
        f"{first_id} -> {last_id}"
    )

    found_tasks = []

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {}

        for task_id in range(
            first_id,
            last_id + 1
        ):
            future = executor.submit(
                probe_task,
                task_id,
            )

            futures[future] = task_id

        for future in as_completed(futures):
            task_id = futures[future]

            try:
                result = future.result()

            except Exception as exc:
                log(
                    f"WORKER ERROR {task_id}: "
                    f"{repr(exc)}"
                )
                continue

            if result is not None:
                found_tasks.append(result)

    found_tasks.sort(
        key=lambda item: item["id"]
    )

    log(
        f"SCAN RESULT: "
        f"{len(found_tasks)} "
        f"existing task(s)"
    )

    for task in found_tasks:
        process_task(
            task,
            state,
        )

    # КЛЮЧОВА ЗМІНА:
    #
    # Навіть якщо в діапазоні було 0 існуючих ID,
    # ми все одно рухаємо вікно вперед.
    #
    # Тобто бот більше НЕ застрягає перед
    # "діркою" з 80+ порожніх ID.

    state["last_scan_id"] = last_id

    save_state(state)

    log(
        f"SCAN POSITION SAVED: "
        f"{last_id}"
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    log("===================================")
    log("KABANCHIK BOT STARTED")
    log(f"POLL_SECONDS = {POLL_SECONDS}")
    log(f"SCAN_AHEAD = {SCAN_AHEAD}")
    log(f"WORKERS = {WORKERS}")
    log(f"STATE_FILE = {STATE_FILE}")
    log("===================================")

    state = load_state()

    log(
        f"START POSITION: "
        f"{state.get('last_scan_id')}"
    )

    log(
        f"ALREADY SENT: "
        f"{len(state.get('sent_ids', []))}"
    )

    # Це дасть нам одразу зрозуміти,
    # що новий deployment реально запустився.
    if send_telegram(
        "🟢 <b>Kabanchik bot запущений</b>\n"
        "Нова версія моніторингу працює."
    ):
        log(
            "START MESSAGE SENT TO TELEGRAM"
        )
    else:
        log(
            "WARNING: START MESSAGE NOT SENT"
        )

    while True:
        started = time.time()

        try:
            scan_cycle(state)

        except Exception as exc:
            log(
                f"CYCLE ERROR: "
                f"{repr(exc)}"
            )

        elapsed = round(
            time.time() - started,
            1
        )

        log(
            f"CYCLE FINISHED IN "
            f"{elapsed}s"
        )

        log(
            f"WAIT {POLL_SECONDS}s"
        )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
