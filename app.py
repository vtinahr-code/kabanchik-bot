from __future__ import annotations
import os, re, time, json, html, logging
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('kabanchik')
TOKEN = os.environ['TELEGRAM_BOT_TOKEN'].strip()
CHAT = os.environ['TELEGRAM_CHAT_ID'].strip()
URL = os.getenv('KABANCHIK_LIST_URL','https://kabanchik.ua/ua/cabinet/dashboard/p/recommended')
COOKIE = os.getenv('KABANCHIK_COOKIE','').strip()
INTERVAL = max(30, int(os.getenv('POLL_SECONDS','60')))
STATE = Path(os.getenv('STATE_FILE','/data/kabanchik_seen.json'))
BASE = 'https://kabanchik.ua'
HEADERS = {'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36','Accept-Language':'uk-UA,uk;q=0.9,ru;q=0.8'}
# Only accounting services, not generic marketing/legal/business tasks.
ACCOUNTING = re.compile(r'бухгалтер|бухгалтерск|облік|уч[её]т|звітн|отч[её]тност|податков|налогов|єсв|ндфл|пдв|ндс|фоп|фопа|фопів|флп|1с|bas\b|медок|m\.e\.doc', re.I)
CLOSED = re.compile(r'закрит[оаий]|виконан[оий]|завершен[оий]|закрыт|выполнен|завершен',re.I)
TASK_PATH = re.compile(r'/((?:ua|ru)/)?task/\d+(?:[/?#]|$)',re.I)

session = requests.Session(); session.headers.update(HEADERS)
if COOKIE: session.headers['Cookie'] = COOKIE

def telegram(text):
    for attempt in range(3):
        try:
            r = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':CHAT,'text':text,'parse_mode':'HTML','disable_web_page_preview':True},timeout=15)
            if r.status_code == 429:
                time.sleep(min(90,int(r.json().get('parameters',{}).get('retry_after',15))+1)); continue
            r.raise_for_status(); return True
        except Exception as e:
            log.error('Telegram send failed: %s',e)
            time.sleep(2)
    return False

def load_seen():
    try: return set(json.loads(STATE.read_text(encoding='utf-8')))
    except (OSError,ValueError): return set()

def save_seen(seen):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    temp=STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(sorted(seen)[-20000:],ensure_ascii=False),encoding='utf-8')
    temp.replace(STATE)

def fetch_tasks():
    r=session.get(URL,timeout=20,allow_redirects=True)
    log.info('SOURCE HTTP=%s final_url=%s bytes=%s',r.status_code,r.url,len(r.content))
    r.raise_for_status()
    if '/auth/' in urlparse(r.url).path or '/login' in urlparse(r.url).path:
        raise RuntimeError('Kabanchik redirected to login. Session authentication is required.')
    soup=BeautifulSoup(r.text,'html.parser')
    # Pages that load orders only through JavaScript will not yield task links here.
    results={}
    for a in soup.select('a[href]'):
        href=urljoin(r.url,a.get('href',''))
        if urlparse(href).netloc not in ('kabanchik.ua','www.kabanchik.ua') or not TASK_PATH.search(urlparse(href).path): continue
        key=re.search(r'/task/(\d+)',urlparse(href).path)
        if not key: continue
        task_id=key.group(1)
        container=a.find_parent(['article','li']) or a.find_parent(class_=re.compile('task|order|item|card',re.I)) or a
        title=a.get_text(' ',strip=True) or container.get_text(' ',strip=True)[:120]
        text=container.get_text(' ',strip=True)[:1500]
        if not ACCOUNTING.search(text): continue
        if CLOSED.search(text): continue
        results[task_id]={'id':task_id,'url':href,'title':title[:180] or 'Бухгалтерське замовлення'}
    return list(results.values())

def main():
    seen=load_seen()
    log.info('START | url=%s | cookie_provided=%s | previously_seen=%s',URL,bool(COOKIE),len(seen))
    telegram('🤖 <b>Бот Kabanchik запущено.</b> Перевіряю бухгалтерські замовлення. Це тестове повідомлення, не нове замовлення.')
    errors=0; last_alert=0
    while True:
        try:
            tasks=fetch_tasks()
            log.info('SCAN DONE | matching=%s | seen=%s',len(tasks),len(seen))
            # On first successful scan, baseline current results to avoid old notifications.
            if not STATE.exists():
                seen.update(t['id'] for t in tasks); save_seen(seen)
                log.info('INITIAL BASELINE | %s tasks; notifications start with next new task',len(tasks))
            else:
                for task in tasks:
                    if task['id'] in seen: continue
                    message='📣 <b>Нове бухгалтерське замовлення</b>\n\n<b>'+html.escape(task['title'])+'</b>\n'+html.escape(task['url'])
                    if telegram(message):
                        seen.add(task['id']); save_seen(seen)
                        log.info('SENT | task=%s',task['id'])
            errors=0
        except Exception as e:
            errors+=1
            log.exception('SCAN ERROR | consecutive=%s | %s',errors,e)
            if errors>=3 and time.time()-last_alert>3600:
                telegram('⚠️ <b>Kabanchik: помилка сканування</b>\n'+html.escape(str(e)[:300])+'\nПеревір Railway Logs.')
                last_alert=time.time()
        time.sleep(INTERVAL)

if __name__=='__main__': main()
