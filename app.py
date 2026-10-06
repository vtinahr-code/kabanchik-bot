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
    main()        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
}

# Слова, за якими нам цікаве замовлення
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

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# ЛОГИ
# ============================================================

def log(message):
    print(
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "|",
        message,
        flush=True
    )


# ============================================================
# STATE
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "last_valid_id": 0,
            "sent_ids": []
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        data.setdefault("last_valid_id", 0)
        data.setdefault("sent_ids", [])

        return data

    except Exception as e:
        log(f"STATE LOAD ERROR: {e}")

        return {
            "last_valid_id": 0,
            "sent_ids": []
        }


def save_state(state):
    try:
        # Не даємо sent_ids рости безкінечно
        state["sent_ids"] = state["sent_ids"][-5000:]

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:
        log(f"STATE SAVE ERROR: {e}")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        r = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if r.ok:
            return True

        log(
            f"TELEGRAM ERROR "
            f"{r.status_code}: {r.text[:300]}"
        )

    except Exception as e:
        log(f"TELEGRAM EXCEPTION: {e}")

    return False


# ============================================================
# KABANCHIK
# ============================================================

def probe_task(task_id):
    """
    Перевіряє один ID.
    Повертає:
        None — замовлення не знайдено
        dict — замовлення знайдено
    """

    url = BASE_URL.format(task_id)

    try:
        r = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

    except Exception as e:
        log(f"REQUEST ERROR {task_id}: {e}")
        return None

    if r.status_code != 200:
        return None

    final_url = r.url.lower()

    # Якщо Kabanchik перекинув не на сторінку task
    if "/task/" not in final_url:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    title = ""

    if soup.title:
        title = soup.title.get_text(
            " ",
            strip=True
        )

    h1 = soup.find("h1")

    if h1:
        title = h1.get_text(
            " ",
            strip=True
        ) or title

    page_text = soup.get_text(
        " ",
        strip=True
    )

    # Захист від сторінок 404 / видалених завдань
    lower = page_text.lower()

    bad_markers = [
        "сторінку не знайдено",
        "страница не найдена",
        "завдання не знайдено",
        "замовлення не знайдено",
        "404",
    ]

    if any(marker in lower for marker in bad_markers):
        return None

    # Реальна сторінка завдання повинна мати хоч якийсь зміст
    if len(page_text) < 150:
        return None

    return {
        "id": task_id,
        "url": url,
        "title": title,
        "text": page_text,
    }


# ============================================================
# ФІЛЬТР БУХГАЛТЕРСЬКИХ ЗАМОВЛЕНЬ
# ============================================================

def is_accounting_task(task):
    text = (
        task.get("title", "")
        + " "
        + task.get("text", "")
    ).lower()

    return any(
        keyword in text
        for keyword in KEYWORDS
    )


def clean_title(title):
    title = re.sub(
        r"\s+",
        " ",
        title or ""
    ).strip()

    return title[:300]


# ============================================================
# ОБРОБКА ЗНАЙДЕНОГО ЗАМОВЛЕННЯ
# ============================================================

def process_task(task, state):
    task_id = task["id"]

    if task_id in state["sent_ids"]:
        return

    if not is_accounting_task(task):
        log(
            f"SKIP {task_id}: "
            f"не бухгалтерське"
        )
        return

    title = clean_title(task["title"])

    log(
        f"NEW ACCOUNTING TASK: "
        f"{task_id} | {title}"
    )

    message = (
        "🔥 <b>Нове замовлення на Kabanchik</b>\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"🆔 {task_id}\n"
        f"🔗 {task['url']}"
    )

    if send_telegram(message):
        state["sent_ids"].append(task_id)

        save_state(state)

        log(f"SENT {task_id}")

    else:
        log(
            f"NOT SENT {task_id}: "
            f"Telegram error"
        )


# ============================================================
# ПОШУК СТАРТОВОГО ID
# ============================================================

def find_start_id():
    """
    Якщо state.json ще немає, пробуємо знайти приблизно
    актуальний ID.

    Значення можна також задати вручну через Railway:
    START_ID
    """

    env_start = os.getenv(
        "START_ID",
        ""
    ).strip()

    if env_start.isdigit():
        return int(env_start)

    # Безпечний fallback.
    # Якщо бот уже працював раніше, state.json повинен містити ID.
    return 0


# ============================================================
# ОДИН ЦИКЛ СКАНУВАННЯ
# ============================================================

def scan_cycle(state):

    last_valid_id = int(
        state.get(
            "last_valid_id",
            0
        )
    )

    if last_valid_id <= 0:
        last_valid_id = find_start_id()

    if last_valid_id <= 0:
        log(
            "ERROR: немає last_valid_id. "
            "Задай START_ID у Railway Variables."
        )

        return

    start_id = last_valid_id + 1
    end_id = last_valid_id + SCAN_AHEAD

    log(
        f"SCAN START: "
        f"{start_id} → {end_id}"
    )

    ids = range(
        start_id,
        end_id + 1
    )

    found_tasks = []

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {
            executor.submit(
                probe_task,
                task_id
            ): task_id

            for task_id in ids
        }

        for future in as_completed(futures):

            task_id = futures[future]

            try:
                task = future.result()

            except Exception as e:
                log(
                    f"WORKER ERROR "
                    f"{task_id}: {e}"
                )
                continue

            if task:
                found_tasks.append(task)

    found_tasks.sort(
        key=lambda x: x["id"]
    )

    log(
        f"SCAN RESULT: "
        f"знайдено {len(found_tasks)} "
        f"реальних замовлень"
    )

    if found_tasks:

        highest_valid_id = max(
            task["id"]
            for task in found_tasks
        )

        for task in found_tasks:
            process_task(
                task,
                state
            )

        if highest_valid_id > state["last_valid_id"]:
            state["last_valid_id"] = highest_valid_id

            save_state(state)

            log(
                f"LAST VALID ID: "
                f"{highest_valid_id}"
            )

    else:
        log(
            "У цьому діапазоні "
            "замовлень не знайдено. "
            "Наступного циклу перевіряємо знову."
        )


# ============================================================
# MAIN
# ============================================================

def main():

    log("===================================")
    log("KABANCHIK BOT STARTED")
    log(f"POLL_SECONDS = {POLL_SECONDS}")
    log(f"SCAN_AHEAD = {SCAN_AHEAD}")
    log(f"WORKERS = {WORKERS}")
    log("===================================")

    state = load_state()

    log(
        f"STATE: last_valid_id="
        f"{state.get('last_valid_id')}, "
        f"sent={len(state.get('sent_ids', []))}"
    )

    # Повідомлення після кожного нового deployment
    send_telegram(
        "🟢 <b>Kabanchik bot запущений</b>\n"
        "Моніторинг бухгалтерських замовлень працює."
    )

    while True:

        cycle_started = time.time()

        try:
            scan_cycle(state)

        except Exception as e:
            log(
                f"CYCLE FATAL ERROR: {repr(e)}"
            )

        elapsed = round(
            time.time() - cycle_started,
            1
        )

        log(
            f"CYCLE FINISHED: "
            f"{elapsed} sec. "
            f"Наступна перевірка через "
            f"{POLL_SECONDS} sec."
        )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
