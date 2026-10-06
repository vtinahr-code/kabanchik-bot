import os
import re
import time
import json
import html
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# НАЛАШТУВАННЯ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Не задані BOT_TOKEN або CHAT_ID у Railway Variables")

BASE_URL = "https://kabanchik.ua/ua/task/{}"

POLL_SECONDS = 60

# Скільки ID дивимося вперед від останнього відомого
SCAN_AHEAD = 1500

# Кількість паралельних запитів
WORKERS = 20

REQUEST_TIMEOUT = 12

STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
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
