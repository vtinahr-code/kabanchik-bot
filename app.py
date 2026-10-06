from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


# ============================================================
# TELEGRAM / RAILWAY
# ============================================================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
REQUEST_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))
WORKERS = int(os.getenv("WORKERS", "12"))

STATE_FILE = Path(
    os.getenv("STATE_FILE_V7", "/data/state_v7.json")
)

START_TASK_ID = int(
    os.getenv("START_TASK_ID_V7", "4997000")
)

SCAN_AHEAD = int(
    os.getenv("SCAN_AHEAD", "800")
)

BASE_TASK_URL = "https://kabanchik.ua/ua/task/{}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8",
}


# ============================================================
# БУХГАЛТЕРСЬКІ ВИДИ ПОСЛУГ KABANCHIK
#
# ВАЖЛИВО:
# перевіряється ВИД ПОСЛУГИ конкретного замовлення,
# а НЕ заголовок і НЕ весь текст сторінки.
# ============================================================

ACCOUNTING_SERVICES = [
    "складання фінансової звітності",
    "ведення бухгалтерського обліку",
    "ведення бухгалтерського обліку підприємства",
    "бухгалтерські послуги",
    "послуги бухгалтера",
    "консультація бухгалтера",
    "бухгалтерська консультація",
    "відновлення бухгалтерського обліку",
    "постановка бухгалтерського обліку",
    "податкова звітність",
    "бухгалтерська звітність",
    "складання податкової звітності",
    "реєстрація податкових накладних",
    "податкові накладні",
    "ведення фоп",
    "звітність фоп",
    "бухгалтерський супровід",
    "бухгалтерський облік та аудит",

    # російські варіанти, якщо Kabanchik віддасть
    # назву самої послуги російською
    "составление финансовой отчетности",
    "ведение бухгалтерского учета",
    "бухгалтерские услуги",
    "услуги бухгалтера",
    "консультация бухгалтера",
    "восстановление бухгалтерского учета",
    "налоговая отчетность",
    "ведение фоп",
]


# ============================================================
# LOG
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

def default_state():
    return {
        "last_scan_id": START_TASK_ID,
        "sent_ids": []
    }


def load_state():

    try:

        if not STATE_FILE.exists():
            log(f"NEW STATE. START ID = {START_TASK_ID}")
            return default_state()

        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)

        if "last_scan_id" not in state:

            old_id = (
                state.get("last_valid_id")
                or state.get("last_task_id")
                or START_TASK_ID
            )

            state["last_scan_id"] = int(old_id)

        state.setdefault("sent_ids", [])

        return state

    except Exception as exc:

        log(f"STATE ERROR: {exc}")

        return default_state()


def save_state(state):

    try:

        STATE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        state["sent_ids"] = state["sent_ids"][-10000:]

        temp_file = STATE_FILE.with_suffix(".tmp")

        with temp_file.open("w", encoding="utf-8") as f:

            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2
            )

        temp_file.replace(STATE_FILE)

    except Exception as exc:

        log(f"STATE SAVE ERROR: {exc}")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    try:

        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15
        )

        if response.ok:
            return True

        log(
            f"TELEGRAM ERROR "
            f"{response.status_code}: "
            f"{response.text[:300]}"
        )

    except Exception as exc:

        log(f"TELEGRAM EXCEPTION: {exc}")

    return False


# ============================================================
# HTML
# ============================================================

def strip_html(source):

    source = re.sub(
        r"(?is)<script.*?>.*?</script>",
        " ",
        source
    )

    source = re.sub(
        r"(?is)<style.*?>.*?</style>",
        " ",
        source
    )

    source = re.sub(
        r"(?s)<[^>]+>",
        "\n",
        source
    )

    source = html.unescape(source)

    lines = []

    for line in source.splitlines():

        line = re.sub(r"\s+", " ", line).strip()

        if line:
            lines.append(line)

    return lines


def extract_title(source):

    match = re.search(
        r"(?is)<h1[^>]*>(.*?)</h1>",
        source
    )

    if match:

        title = re.sub(
            r"<[^>]+>",
            " ",
            match.group(1)
        )

        title = html.unescape(title)

        title = re.sub(
            r"\s+",
            " ",
            title
        ).strip()

        return title

    match = re.search(
        r"(?is)<title[^>]*>(.*?)</title>",
        source
    )

    if match:

        title = html.unescape(match.group(1))

        return re.sub(
            r"\s+",
            " ",
            title
        ).strip()

    return "Нове замовлення"


# ============================================================
# ГОЛОВНЕ:
# ДІСТАЄМО ЛИШЕ ВИД ПОСЛУГИ З ОСНОВНОГО БЛОКУ
# ============================================================

def extract_main_block(lines):

    result = []

    for line in lines:

        if line.lower().startswith(
            "інші замовлення у категорії"
        ):
            break

        result.append(line)

    return result


def detect_accounting_service(lines):

    main_lines = extract_main_block(lines)

    # Не дивимося рекомендації, футер,
    # "Також створювали" тощо.
    #
    # Перевіряємо тільки основний блок
    # конкретного замовлення.

    for line in main_lines:

        normalized = line.lower().strip()

        for service in ACCOUNTING_SERVICES:

            if (
                normalized == service
                or normalized.startswith(service)
            ):

                return line

    return None


# ============================================================
# TASK
# ============================================================

def probe_task(task_id):

    url = BASE_TASK_URL.format(task_id)

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    # Kabanchik може редіректити неіснуючий ID
    if "/task/" not in response.url:
        return None

    lines = strip_html(response.text)

    if len(lines) < 10:
        return None

    joined = " ".join(lines).lower()

    NOT_FOUND = [
        "сторінку не знайдено",
        "сторінка не знайдена",
        "страница не найдена",
        "завдання не знайдено",
        "page not found"
    ]

    if any(x in joined for x in NOT_FOUND):
        return None

    title = extract_title(response.text)

    service = detect_accounting_service(lines)

    return {
        "id": task_id,
        "url": response.url,
        "title": title,
        "service": service
    }


# ============================================================
# PROCESS
# ============================================================

def process_task(task, state):

    task_id = int(task["id"])

    if task_id in state["sent_ids"]:
        return

    # ========================================================
    # НАЙВАЖЛИВІШИЙ ФІЛЬТР
    # ========================================================

    if not task["service"]:

        log(
            f"SKIP {task_id}: "
            f"NOT ACCOUNTING"
        )

        return

    # ========================================================

    title = task["title"]

    service = task["service"]

    log(
        f"ACCOUNTING FOUND: "
        f"{task_id} | {service} | {title}"
    )

    message = (
        "🔥 <b>Нове бухгалтерське замовлення</b>\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"📂 {html.escape(service)}\n"
        f"🆔 {task_id}\n\n"
        f"🔗 {html.escape(task['url'])}"
    )

    if send_telegram(message):

        state["sent_ids"].append(task_id)

        save_state(state)

        log(f"SENT: {task_id}")

    else:

        log(
            f"SEND FAILED: {task_id}"
        )


# ============================================================
# SCANNER
# ============================================================

def scan_cycle(state):

    current = int(
        state.get(
            "last_scan_id",
            START_TASK_ID
        )
    )

    first_id = current + 1
    last_id = current + SCAN_AHEAD

    log(
        f"SCAN {first_id} -> {last_id}"
    )

    found = []

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {
            executor.submit(
                probe_task,
                task_id
            ): task_id

            for task_id in range(
                first_id,
                last_id + 1
            )
        }

        for future in as_completed(futures):

            try:

                task = future.result()

                if task:
                    found.append(task)

            except Exception as exc:

                log(
                    f"WORKER ERROR: {exc}"
                )

    found.sort(
        key=lambda x: x["id"]
    )

    log(
        f"EXISTING TASKS FOUND: "
        f"{len(found)}"
    )

    for task in found:

        process_task(
            task,
            state
        )

    state["last_scan_id"] = last_id

    save_state(state)

    log(
        f"SCAN FINISHED AT {last_id}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    log("===================================")
    log("KABANCHIK ACCOUNTING BOT STARTED")
    log("FILTER: ACCOUNTING SERVICES ONLY")
    log("===================================")

    state = load_state()

    log(
        f"START POSITION: "
        f"{state['last_scan_id']}"
    )

    send_telegram(
        "🟢 <b>Kabanchik bot запущений</b>\n\n"
        "Фільтр: тільки бухгалтерські послуги."
    )

    while True:

        try:

            scan_cycle(state)

        except Exception as exc:

            log(
                f"CYCLE ERROR: {repr(exc)}"
            )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
