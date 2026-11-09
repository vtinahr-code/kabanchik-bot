from __future__ import annotations
import base64, html, json, logging, os, re, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('kabanchik')
TOKEN = os.environ['TELEGRAM_BOT_TOKEN'].strip()
CHAT = os.environ['TELEGRAM_CHAT_ID'].strip()
SOURCE = os.getenv('KABANCHIK_LIST_URL', 'https://kabanchik.ua/ua/cabinet/dashboard/p/recommended')
INTERVAL = max(30, int(os.getenv('POLL_SECONDS', '60')))
STATE = Path(os.getenv('STATE_FILE', '/tmp/kabanchik_seen.json'))
ACCOUNTING = re.compile(r'бухгалтер|бухгалтерск|облік|уч[её]т|звітн|отч[её]тност|податков|налогов|єсв|ндфл|пдв|ндс|фоп|флп|\b1с\b|\bbas\b|медок|m\.e\.doc', re.I)
CLOSED = re.compile(r'закрит[оаий]|виконан[оий]|завершен[оий]|закрыт|выполнен|завершен', re.I)
TASK_PATH = re.compile(r'/(?:ua/|ru/)?task/(\d+)(?:[/?#]|$)', re.I)


def telegram(message):
    for attempt in range(3):
        try:
            r = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={'chat_id': CHAT, 'text': message, 'parse_mode': 'HTML', 'disable_web_page_preview': True}, timeout=15)
            r.raise_for_status()
            return True
        except Exception as exc:
            log.warning('Telegram error: %s', exc)
            time.sleep(2)
    return False


def storage_state():
    encoded = os.getenv('KABANCHIK_SESSION_B64', '').strip()
    if not encoded:
        raise RuntimeError('KABANCHIK_SESSION_B64 is missing in Railway Variables')
    try:
        raw = base64.b64decode(encoded, validate=True)
        state = json.loads(raw)
        if not isinstance(state, dict) or not isinstance(state.get('cookies'), list):
            raise ValueError('Expected Playwright storage state with cookies')
        return state
    except Exception as exc:
        raise RuntimeError('Invalid KABANCHIK_SESSION_B64: expected base64-encoded Playwright storage_state JSON') from exc


def read_seen():
    try:
        return set(json.loads(STATE.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return set()


def save_seen(seen):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding='utf-8')
    temp.replace(STATE)


def scan(page):
    page.goto(SOURCE, wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(4000)
    url = page.url
    if re.search(r'/auth/|/login|/signin', url, re.I):
        raise RuntimeError('Kabanchik redirected to login: saved session is not accepted or expired')
    # Wait for JS-rendered cards; no guarantee the recommended feed covers every city.
    anchors = page.locator('a[href]')
    found = {}
    for item in anchors.evaluate_all('''els => els.map(a => ({href: a.href, title: (a.innerText || '').trim(), text: (a.closest('article, li, [class*="task"], [class*="order"], [class*="card"]') || a.parentElement || a).innerText || ''}))'''):
        href = item.get('href', '')
        parsed = urlparse(href)
        if parsed.hostname not in ('kabanchik.ua', 'www.kabanchik.ua'):
            continue
        match = TASK_PATH.search(parsed.path)
        if not match:
            continue
        content = (item.get('title', '') + ' ' + item.get('text', ''))[:2000]
        if not ACCOUNTING.search(content) or CLOSED.search(content):
            continue
        task_id = match.group(1)
        found[task_id] = {'id': task_id, 'url': href, 'title': (item.get('title') or content[:140] or 'Бухгалтерське замовлення')[:180]}
    log.info('SCAN | url=%s | links=%s | matching=%s', url, anchors.count(), len(found))
    return list(found.values())


def main():
    state = storage_state()
    seen = read_seen()
    log.info('START | interval=%s | previously_seen=%s | session_cookies=%s', INTERVAL, len(seen), len(state['cookies']))
    telegram('🤖 <b>Kabanchik bot запущено.</b> Перевіряю доступ до замовлень.')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(storage_state=state, locale='uk-UA')
        page = context.new_page()
        errors = 0
        last_alert = 0
        while True:
            try:
                tasks = scan(page)
                if not STATE.exists():
                    seen.update(t['id'] for t in tasks)
                    save_seen(seen)
                    log.info('INITIAL BASELINE | %s current tasks', len(tasks))
                else:
                    for task in tasks:
                        if task['id'] in seen:
                            continue
                        msg = '📣 <b>Нове бухгалтерське замовлення</b>\n\n<b>' + html.escape(task['title']) + '</b>\n' + html.escape(task['url'])
                        if telegram(msg):
                            seen.add(task['id'])
                            save_seen(seen)
                            log.info('SENT | task=%s', task['id'])
                errors = 0
            except Exception as exc:
                errors += 1
                log.exception('SCAN ERROR | consecutive=%s | %s', errors, exc)
                if errors >= 3 and time.time() - last_alert > 3600:
                    telegram('⚠️ <b>Kabanchik: помилка сканування</b>\n' + html.escape(str(exc)[:300]))
                    last_alert = time.time()
            time.sleep(INTERVAL)

if __name__ == '__main__':
    main()
