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
# RAILWAY / TELEGRAM
# ============================================================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
REQUEST_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))
WORKERS = int(os.getenv("WORKERS", "10"))

STATE_FILE = Path(
    os.getenv("STATE_FILE_V7", "/data/state_v7.json")
)

START_TASK_ID = int(
    os.getenv("START_TASK_ID_V7", "4997000")
)

SCAN_AHEAD = int(
    os.getenv("SCAN_AHEAD", "500")
)

BASE_TASK_URL = "https://kabanchik.ua/ua/task/{}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
}


# ============================================================
# БУХГАЛТЕРСЬКІ ВИДИ ПОСЛУГ
#
# Перевіряємо ТІЛЬКИ основний блок конкретного замовлення.
# Заголовок замовника може бути українською або російською —
# це не має значення.
# ============================================================

ACCOUNTING_SERVICE_MARKERS = (
    # українські
    "складання фінансової звітності",
    "ведення бухгалтерського обліку",
    "бухгалтерські послуги",
    "послуги бухгалтера",
    "консультація бухгалтера",
    "консультації бухгалтера",
    "бухгалтерська консультація",
    "відновлення бухгалтерського обліку",
    "постановка бухгалтерського обліку",
    "податкова звітність",
    "бухгалтерська звітність",
    "складання податкової звітності",
    "реєстрація податкових накладних",
    "бухгалтерський супровід",
    "ведення фоп",
    "звітність фоп",
    "аудит бухгалтерського обліку",

    # російські
    "составление финансовой отчетности",
    "ведение бухгалтерского учета",
    "бухгалтерские услуги",
    "услуги бухгалтера",
    "консультация бухгалтера",
    "консультации бухгалтера",
    "бухгалтерская консультация",
    "восстановление бухгалтерского учета",
    "постановка бухгалтерского учета",
    "налоговая отчетность",
    "бухгалтерская отчетность",
    "составление налоговой отчетности",
    "регистрация налоговых накладных",
    "бухгалтерское сопровождение",
    "ведение фоп",
    "отчетность фоп",
)


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
            log(f"NEW STATE | START={START_TASK_ID}")
            return default_state()

        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)

        if "last_scan_id" not in state:
            state["last_scan_id"] = int(
                state.get("last_valid_id")
                or state.get("last_task_id")
                or START_TASK_ID
            )

        state.setdefault("sent_ids", [])

        state["sent_ids"] = [
            int(x) for x in state["sent_ids"]
            if str(x).isdigit()
        ]

        return state

    except Exception as exc:
        log(f"STATE LOAD ERROR: {repr(exc)}")
        return default_state()


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        state["sent_ids"] = state["sent_ids"][-10000:]

        temp = STATE_FILE.with_suffix(".tmp")

        with temp.open("w", encoding="utf-8") as f:
            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2
            )

        temp.replace(STATE_FILE)

    except Exception as exc:
        log(f"STATE SAVE ERROR: {repr(exc)}")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    while True:
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

            # Telegram тимчасово блокує після великої
            # кількості повідомлень.
            if response.status_code == 429:
                try:
                    data = response.json()

                    retry_after = int(
                        data.get(
                            "parameters", {}
                        ).get(
                            "retry_after", 60
                        )
                    )

                except Exception:
                    retry_after = 60

                log(
                    f"TELEGRAM RATE LIMIT | "
                    f"WAIT {retry_after}s"
                )

                time.sleep(retry_after + 2)

                # Повторюємо САМЕ це повідомлення.
                continue

            log(
                f"TELEGRAM ERROR "
                f"{response.status_code}"
            )

            return False

        except Exception as exc:
            log(
                f"TELEGRAM EXCEPTION: "
                f"{repr(exc)}"
            )

            return False


# ============================================================
# HTML
# ============================================================

def html_to_lines(source):
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

    result = []

    for line in source.splitlines():
        line = re.sub(
            r"\s+",
            " ",
            line
        ).strip()

        if line:
            result.append(line)

    return result


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

        return re.sub(
            r"\s+",
            " ",
            title
        ).strip()

    match = re.search(
        r"(?is)<title[^>]*>(.*?)</title>",
        source
    )

    if match:
        title = html.unescape(
            match.group(1)
        )

        return re.sub(
            r"\s+",
            " ",
            title
        ).strip()

    return "Нове замовлення"


# ============================================================
# ОСНОВНИЙ БЛОК ЗАМОВЛЕННЯ
#
# ВСЕ після "Інші замовлення у категорії"
# НЕ перевіряємо.
# ============================================================

def get_main_task_lines(lines):
    result = []

    for line in lines:
        normalized = line.lower()

        if "інші замовлення у категорії" in normalized:
            break

        if "другие заказы в категории" in normalized:
            break

        result.append(line)

    return result


# ============================================================
# ВЕРХНЯ КАТЕГОРІЯ KABANCHIK
# ============================================================

def get_parent_category(lines):
    for line in lines:
        normalized = line.lower()

        if (
            "хочете виконувати замовлення у категорії"
            in normalized
        ):
            return line

        if (
            "хотите выполнять заказы в категории"
            in normalized
        ):
            return line

    return ""


# ============================================================
# БУХГАЛТЕРСЬКИЙ ФІЛЬТР
# ============================================================

def detect_accounting_service(lines):
    # Перша страховка:
    # замовлення повинно бути хоча б у "Ділових послугах".
    parent_category = get_parent_category(lines).lower()

    if parent_category:
        business_parent = (
            "ділові послуги" in parent_category
            or
            "деловые услуги" in parent_category
        )

        if not business_parent:
            return None

    # Друга, головна перевірка:
    # дивимось ТІЛЬКИ на основний блок
    # конкретного замовлення.
    main_lines = get_main_task_lines(lines)

    # На сторінках Kabanchik тип послуги знаходиться
    # окремим рядком перед описом замовлення.
    #
    # Перевіряємо окремі рядки, а НЕ весь HTML.
    for line in main_lines:
        normalized = line.lower().strip()

        for marker in ACCOUNTING_SERVICE_MARKERS:
            if (
                normalized == marker
                or normalized.startswith(marker + ":")
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

    if "/task/" not in response.url:
        return None

    lines = html_to_lines(
        response.text
    )

    if len(lines) < 10:
        return None

    first_part = " ".join(
        lines[:50]
    ).lower()

    not_found = (
        "сторінку не знайдено",
        "сторінка не знайдена",
        "страница не найдена",
        "завдання не знайдено",
        "page not found",
    )

    if any(
        marker in first_part
        for marker in not_found
    ):
        return None

    title = extract_title(
        response.text
    )

    service = detect_accounting_service(
        lines
    )

    return {
        "id": task_id,
        "url": response.url,
        "title": title,
        "service": service,
    }


# ============================================================
# PROCESS
# ============================================================

def process_task(task, state):
    task_id = int(task["id"])

    if task_id in state["sent_ids"]:
        return

    # НЕ бухгалтерське —
    # просто мовчимо.
    # Ніяких тисяч SKIP у Railway.
    if not task["service"]:
        return

    title = task["title"]
    service = task["service"]

    log(
        f"ACCOUNTING FOUND | "
        f"{task_id} | "
        f"{service}"
    )

    message = (
        "🔥 <b>Нове замовлення — "
        "Бухгалтерські послуги</b>\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"📂 {html.escape(service)}\n"
        f"🆔 {task_id}\n\n"
        f"🔗 {html.escape(task['url'])}"
    )

    if send_telegram(message):
        state["sent_ids"].append(
            task_id
        )

        save_state(state)

        log(
            f"SENT TO TELEGRAM | "
            f"{task_id}"
        )


# ============================================================
# SCAN
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
        f"SCAN | "
        f"{first_id}-{last_id}"
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

        for future in as_completed(
            futures
        ):
            try:
                task = future.result()

                if task:
                    found.append(task)

            except Exception:
                pass

    found.sort(
        key=lambda item: item["id"]
    )

    accounting_count = 0

    for task in found:
        if task["service"]:
            accounting_count += 1

        process_task(
            task,
            state
        )

    state["last_scan_id"] = last_id

    save_state(state)

    log(
        f"SCAN DONE | "
        f"existing={len(found)} | "
        f"accounting={accounting_count}"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    log(
        "KABANCHIK BOT STARTED | "
        "ACCOUNTING ONLY"
    )

    state = load_state()

    log(
        f"START POSITION | "
        f"{state['last_scan_id']}"
    )

    # Не шлемо стартове повідомлення в Telegram.
    # Telegram — тільки для реальних
    # бухгалтерських замовлень.

    while True:
        try:
            scan_cycle(state)

        except Exception as exc:
            log(
                f"CYCLE ERROR: "
                f"{repr(exc)}"
            )

        time.sleep(
            POLL_SECONDS
        )


if __name__ == "__main__":
    main()
