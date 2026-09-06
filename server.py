import json, sqlite3, time, io, csv, threading
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
import httpx
import re

DB = Path(__file__).parent / 'data.db'
TG_BOT = '8980807692:AAE7oobpuegrE0zKgcd5roZiWTsK6tE3qDY'
TG_CHAT = '1868246682'
SCHED_LOCK = threading.Lock()

app = FastAPI(title='Scrapling Dashboard v2')

def db_init():
    con = sqlite3.connect(DB)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS scrapes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT, mode TEXT, status TEXT, item_count INTEGER,
        data_json TEXT, error TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS schedules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT, mode TEXT, selector TEXT, attr TEXT,
        interval_min INTEGER, last_run TEXT, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    ''')
    con.commit(); con.close()
db_init()

# ---------- Scrapling lazy imports ----------
def get_fetchers():
    from scrapling.fetchers import Fetcher, StealthyFetcher, DynamicFetcher
    return Fetcher, StealthyFetcher, DynamicFetcher

def do_fetch(url, mode, timeout=60000):
    Fetcher, StealthyFetcher, DynamicFetcher = get_fetchers()
    if mode == 'http':
        return Fetcher.get(url, timeout=timeout)
    if mode == 'stealth':
        return StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=timeout)
    return DynamicFetcher.fetch(url, headless=True, network_idle=True, timeout=timeout)

# ---------- Extraction ----------
def basic_extract(page, selector, attr):
    items = []
    if selector:
        els = page.css(selector)
        for el in els:
            v = el.text if attr == 'text' else el.attrib.get(attr, '')
            if v and str(v).strip(): items.append(str(v).strip())
        if items: return items
    # fallback: links + images + text blocks
    for a in page.css('a'):
        t, href = (a.text or '').strip(), a.attrib.get('href', '')
        if t and href: items.append({'text': t[:120], 'href': href})
    for img in page.css('img'):
        src = img.attrib.get('src', '')
        if src and 'loading' not in src: items.append({'image': src})
    return items

def smart_extract(page):
    out = {'page_title': '', 'meta_desc': '', 'headings': [], 'menu': [], 'tables': [], 'images': [], 'links': []}
    t = page.css_first('title')
    if t: out['page_title'] = t.text
    m = page.css_first('meta[name=description]')
    if m: out['meta_desc'] = m.attrib.get('content', '')
    for h in page.css('h1, h2, h3'):
        txt = h.text.strip()
        if txt: out['headings'].append({'tag': h.tag, 'text': txt[:150]})
    seen = set()
    for a in page.css('nav a, header a, .menu a, #menu a, .navbar a'):
        label, href = (a.text or '').strip(), a.attrib.get('href', '')
        if label and (label, href) not in seen:
            seen.add((label, href)); out['menu'].append({'label': label[:80], 'href': href})
    for tb in page.css('table'):
        rows = []
        for tr in tb.css('tr'):
            cells = [td.text.strip() for td in tr.css('th, td')]
            if any(cells): rows.append(cells)
        if rows: out['tables'].append(rows)
    seen_img = set()
    for img in page.css('img'):
        src = img.attrib.get('src', '')
        if src and src not in seen_img and 'loading' not in src:
            seen_img.add(src); out['images'].append({'src': src, 'alt': img.attrib.get('alt', '')[:100]})
    seen_l = set()
    for a in page.css('a'):
        label, href = (a.text or '').strip(), a.attrib.get('href', '')
        if href and label and len(label) > 20 and (label, href) not in seen_l:
            seen_l.add((label, href)); out['links'].append({'text': label[:150], 'href': href})
    return {k: v for k, v in out.items() if v}

# ---------- AI extraction via 9router (OpenAI-compatible) ----------
def ai_extract(html_text, prompt):
    sys = ('You are a web scraping data extractor. Extract structured data as JSON array from the HTML content '
           'based on user instructions. Return ONLY a JSON array, no markdown, no explanation.')
    messages = [{'role': 'system', 'content': sys},
                {'role': 'user', 'content': f'Instruction: {prompt}\n\nHTML (truncated):\n{html_text[:15000]}'}]
    headers = {'Authorization': 'Bearer sk-6b3ac6ef8e3b70c9-vr88tq-8a4cba7f',
               'Content-Type': 'application/json'}
    last_err = None
    for attempt in range(1, 4):
        try:
            body = {'model': MODELS[attempt % len(MODELS)], 'messages': messages, 'temperature': 0.1}
            r = httpx.post('https://9router.haviq.dev/v1/chat/completions', json=body, headers=headers, timeout=120)
            r.raise_for_status()
            clean_body = re.sub(r'data:\s*\[DONE\]', '', r.text).strip()
            raw = (json.loads(clean_body)['choices'][0]['message'].get('content') or '').strip()
            if raw.startswith('```'):
                raw = raw.split('```')[1].lstrip('json').strip()
            start_idx, end_idx = raw.find('['), raw.rfind(']')
            if start_idx == -1 or end_idx <= start_idx:
                raise ValueError('no JSON array in response')
            segment = raw[start_idx:]
            try:
                return json.loads(segment)
            except json.JSONDecodeError:
                cleaned = re.sub(r',\s*([\]\}])', r'\1', segment)
                try:
                    return json.loads(cleaned)
                except json.JSONDecodeError:
                    dec = json.JSONDecoder()
                    obj, _ = dec.raw_decode(segment)
                    return obj
        except Exception as e:
            last_err = e
            time.sleep(3)
    raise last_err or RuntimeError('AI extraction failed')

MODELS = ['buwatqwen', 'baicombo', 'Qwenfree']

# ---------- Scheduler ----------
# ---------- Models ----------
class ScrapeReq(BaseModel):
    url: str
    mode: str = 'http'
    selector: Optional[str] = None
    attr: str = 'text'
    prompt: Optional[str] = None
    paginate: int = 1
    notify: bool = True
class SchedReq(BaseModel):
    url: str; mode: str = 'http'; selector: Optional[str] = None
    attr: str = 'text'; interval_min: int = 60

# ---------- Telegram ----------
def tg_send(text):
    try:
        httpx.post(f'https://api.telegram.org/bot{TG_BOT}/sendMessage',
                   json={'chat_id': TG_CHAT, 'text': text[:4000], 'parse_mode': 'HTML'}, timeout=15)
    except Exception: pass

def save_scrape(url, mode, status, items=None, error=None):
    con = sqlite3.connect(DB)
    con.execute('INSERT INTO scrapes(url,mode,status,item_count,data_json,error) VALUES(?,?,?,?,?,?)',
                (url, mode, status, len(items or []), json.dumps(items or [], ensure_ascii=False), (error or '')[:500]))
    sid = con.execute('SELECT last_insert_rowid()').fetchone()[0]
    con.commit(); con.close(); return sid

def update_scrape(sid, status, items=None, error=None):
    con = sqlite3.connect(DB)
    con.execute('UPDATE scrapes SET status=?, item_count=?, data_json=?, error=? WHERE id=?',
                (status, len(items or []), json.dumps(items or [], ensure_ascii=False), (error or '')[:500], sid))
    con.commit(); con.close()

def job_scrape(sid, url, mode, selector, attr, prompt, paginate, notify):
    try:
        items = fetch_and_extract(url, mode, selector, attr, prompt, paginate)
        update_scrape(sid, 'done', items)
        if notify:
            tg_send(f'🕷️ <b>Scrape selesai #{sid}</b>\nURL: {url}\nItems: {len(items)}\n\nhttps://scrapdash.haviq.dev')
    except Exception as e:
        update_scrape(sid, 'error', error=str(e))
        if notify: tg_send(f'❌ <b>Scrape gagal #{sid}</b>\n{url}\n{str(e)[:200]}')

def fetch_and_extract(url, mode, selector, attr, prompt=None, paginate=1):
    page = do_fetch(url, mode)
    # JSON response detection (BMKG, Epic, dsb.)
    try:
        jd = page.json()
        if jd:
            items_raw = jd if isinstance(jd, list) else [jd]
            flat = []
            def walk(obj):
                if isinstance(obj, dict):
                    flat.append({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)) for k, v in obj.items()})
                elif isinstance(obj, list):
                    for x in obj:
                        walk(x)
            walk(items_raw)
            if flat:
                # Kalau cuma 1 item tapi ada nested array, pecah jadi per-item
                if len(flat) == 1:
                    for v in flat[0].values():
                        try:
                            nested = json.loads(v)
                            if isinstance(nested, list) and nested:
                                flat = []
                                walk(nested)
                                break
                            elif isinstance(nested, dict):
                                # dict berisi key -> list of dicts: walk list-nya
                                for val in nested.values():
                                    if isinstance(val, list) and val and isinstance(val[0], dict):
                                        flat = []
                                        walk(val)
                                        break
                                break
                        except Exception:
                            pass
                return flat
    except Exception:
        pass
    if not selector and not prompt:
        return smart_extract(page)
    if prompt:
        return ai_extract(page.body if hasattr(page, 'body') else page.html_content, prompt)
    items = basic_extract(page, selector, attr)
    if paginate > 1:
        import re
        base = url.rstrip('/')
        patterns = [base + '/{n}', base + '?page={n}', re.sub(r'(-\d+)$', r'-{n}', base)]
        done = {url}
        for n in range(2, paginate + 1):
            for pat in patterns:
                nxt = pat.format(n=n)
                if nxt in done: continue
                try:
                    pg2 = do_fetch(nxt, mode)
                    items.extend(basic_extract(pg2, selector, attr)); done.add(nxt); break
                except Exception:
                    continue
    return items

# ---------- Routes ----------
@app.post('/api/scrape')
def api_scrape(req: ScrapeReq, bg: BackgroundTasks):
    sid = save_scrape(req.url, req.mode, 'running')
    bg.add_task(job_scrape, sid, req.url, req.mode, req.selector, req.attr, req.prompt, req.paginate, req.notify)
    return {'id': sid, 'status': 'running'}

@app.get('/api/history')
def history():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute('SELECT id,url,mode,status,item_count,error,created_at FROM scrapes ORDER BY id DESC LIMIT 200').fetchall()
    con.close(); return [dict(r) for r in rows]

@app.get('/api/history/{sid}')
def detail(sid: int):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    r = con.execute('SELECT * FROM scrapes WHERE id=?', (sid,)).fetchone(); con.close()
    if not r: raise HTTPException(404)
    d = dict(r); d['data'] = json.loads(d['data_json'] or '[]'); return d

@app.delete('/api/history/{sid}')
def del_scrape(sid: int):
    con = sqlite3.connect(DB); con.execute('DELETE FROM scrapes WHERE id=?', (sid,)); con.commit(); con.close()
    return {'ok': True}

@app.get('/api/download/{sid}')
def download(sid: int, format: str = 'csv'):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    r = con.execute('SELECT * FROM scrapes WHERE id=?', (sid,)).fetchone(); con.close()
    if not r: raise HTTPException(404)
    data = json.loads(r['data_json'] or '[]')
    if format == 'json':
        buf = json.dumps(data, ensure_ascii=False, indent=2)
        media, fname = 'application/json', f'scrape_{sid}.json'
    elif format == 'xlsx':
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        wb = Workbook(); ws = wb.active; ws.title = 'Hasil Scrape'
        norm = [row if isinstance(row, dict) else {'value': str(row)} for row in data]
        keys = []
        for row in norm:
            for k in row.keys():
                if k not in keys: keys.append(k)
        ws.append([k.upper() for k in keys])
        for cell in ws[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill(start_color='0D9488', end_color='0D9488', fill_type='solid')
            cell.alignment = Alignment(horizontal='center')
        for row in norm: ws.append([row.get(k, '') for k in keys])
        for i, k in enumerate(keys, 1):
            ws.column_dimensions[get_column_letter(i)].width = max(15, min(50, len(k) + 15))
        ws.freeze_panes = 'A2'
        buf_io = io.BytesIO(); wb.save(buf_io); buf = buf_io.getvalue()
        media, fname = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', f'scrape_{sid}.xlsx'
    else:
        out = io.StringIO()
        norm = [row if isinstance(row, dict) else {'value': str(row)} for row in data]
        keys = []
        for row in norm:
            for k in row.keys():
                if k not in keys: keys.append(k)
        w = csv.DictWriter(out, fieldnames=keys, extrasaction='ignore'); w.writeheader()
        for row in norm: w.writerow(row)
        buf = out.getvalue(); media, fname = 'text/csv', f'scrape_{sid}.csv'
    return StreamingResponse(iter([buf] if isinstance(buf, str) else [buf]), media_type=media,
        headers={'Content-Disposition': f'attachment; filename={fname}'})

@app.post('/api/telegram/{sid}')
def send_tg(sid: int):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    r = con.execute('SELECT * FROM scrapes WHERE id=?', (sid,)).fetchone(); con.close()
    if not r: raise HTTPException(404)
    data = json.loads(r['data_json'] or '[]')
    preview = '\n'.join(json.dumps(x, ensure_ascii=False)[:100] if isinstance(x, dict) else str(x)[:100] for x in data[:15])
    tg_send(f"🕷️ <b>Hasil Scrape #{sid}</b>\n{r['url']}\n\n<pre>{preview}</pre>\n\nTotal: {r['item_count']} items")
    return {'ok': True}

@app.post('/api/schedules')
def add_schedule(req: SchedReq):
    con = sqlite3.connect(DB)
    con.execute('INSERT INTO schedules(url,mode,selector,attr,interval_min) VALUES(?,?,?,?,?)',
                (req.url, req.mode, req.selector, req.attr, req.interval_min))
    con.commit(); con.close(); return {'ok': True}

@app.get('/api/schedules')
def list_schedules():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute('SELECT * FROM schedules ORDER BY id DESC').fetchall(); con.close()
    return [dict(r) for r in rows]

@app.delete('/api/schedules/{sid}')
def del_schedule(sid: int):
    con = sqlite3.connect(DB); con.execute('DELETE FROM schedules WHERE id=?', (sid,)); con.commit(); con.close()
    return {'ok': True}

@app.get('/', response_class=HTMLResponse)
def dashboard():
    return Path('dashboard.html').read_text()

def scheduler_loop():
    import datetime
    while True:
        try:
            con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
            rows = con.execute('SELECT * FROM schedules WHERE active=1').fetchall(); con.close()
            now = datetime.datetime.now().isoformat(timespec='seconds')
            for r in rows:
                last = r['last_run'] or ''
                due = (not last) or (datetime.datetime.fromisoformat(last) + datetime.timedelta(minutes=r['interval_min']) <= datetime.datetime.now())
                if due:
                    with SCHED_LOCK:
                        con = sqlite3.connect(DB)
                        con.execute('UPDATE schedules SET last_run=? WHERE id=?', (now, r['id']))
                        con.commit(); con.close()
                    try:
                        page = do_fetch(r['url'], r['mode'])
                        items = (smart_extract(page) if not r['selector'] else basic_extract(page, r['selector'], r['attr']))
                        save_scrape(r['url'], r['mode'], 'done', items)
                        tg_send(f'⏰ <b>Auto-scrape #{r["id"]}</b>\n{r["url"]}\nItems: {len(items)}')
                    except Exception as e:
                        save_scrape(r['url'], r['mode'], 'error', error=str(e))
        except Exception: pass
        time.sleep(60)

@app.post('/api/schedules')
def add_schedule(req: SchedReq):
    con = sqlite3.connect(DB)
    con.execute('INSERT INTO schedules(url,mode,selector,attr,interval_min) VALUES(?,?,?,?,?)',
                (req.url, req.mode, req.selector, req.attr, req.interval_min))
    con.commit(); con.close(); return {'ok': True}

@app.get('/api/schedules')
def list_schedules():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute('SELECT * FROM schedules ORDER BY id DESC').fetchall(); con.close()
    return [dict(r) for r in rows]

@app.delete('/api/schedules/{sid}')
def del_schedule(sid: int):
    con = sqlite3.connect(DB); con.execute('DELETE FROM schedules WHERE id=?', (sid,)); con.commit(); con.close()
    return {'ok': True}

@app.get('/', response_class=HTMLResponse)
def dashboard():
    return Path('dashboard.html').read_text()

# ---------- start scheduler thread ----------
threading.Thread(target=scheduler_loop, daemon=True).start()
