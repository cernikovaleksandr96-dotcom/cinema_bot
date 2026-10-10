import asyncio, hmac, hashlib, json, os, sqlite3, random, io, csv, time, secrets, re, ipaddress, socket
from html import unescape
from datetime import date, datetime
from urllib.parse import parse_qsl, urlparse, urljoin, urlencode
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import MenuButtonWebApp, BufferedInputFile, Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, CallbackQuery
from dotenv import load_dotenv
import logging
from balancer_service import get_anime_player, get_movie_balancer, find_shikimori_id

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")
BASE = os.path.dirname(os.path.abspath(__file__))
REMIND_HOUR = int(os.getenv("REMIND_HOUR", 19))
TOKEN, TMDB, PORT = os.environ["BOT_TOKEN"], os.environ["TMDB_KEY"], int(os.getenv("PORT", 8080))
URL = os.getenv("WEBAPP_URL", "auto")

# ───────────────────────── БД ─────────────────────────
db = sqlite3.connect("cinema.db"); db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")       # чтения не блокируют запись
db.execute("PRAGMA synchronous=NORMAL")     # коммит без лишнего fsync
db.executescript("""
CREATE TABLE IF NOT EXISTS items(uid INT, tid INT, type TEXT, title TEXT, year TEXT, poster TEXT, genres TEXT,
 runtime INT DEFAULT 0, status TEXT, rating REAL, fav INT DEFAULT 0, date TEXT, mood TEXT, note TEXT,
 season INT DEFAULT 1, episode INT DEFAULT 0, PRIMARY KEY(uid,tid,type));
CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, name TEXT, remind INT DEFAULT 1, last TEXT);
CREATE TABLE IF NOT EXISTS friends(a INT, b INT, PRIMARY KEY(a,b));
CREATE TABLE IF NOT EXISTS dismissed(uid INT, tid INT, type TEXT, PRIMARY KEY(uid,tid,type));
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, uid INT, text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, a INT, b INT, text TEXT, ts INT, rd INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, host INT, guest INT, status TEXT, url TEXT, kind TEXT, vid TEXT,
 hr INT DEFAULT 0, gr INT DEFAULT 0, started INT DEFAULT 0, playing INT DEFAULT 0, pos REAL DEFAULT 0, upd REAL DEFAULT 0,
 ver INT DEFAULT 1, created REAL, hseen REAL DEFAULT 0, gseen REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_sess_h ON sessions(status,host);
CREATE INDEX IF NOT EXISTS ix_sess_g ON sessions(status,guest);
CREATE TABLE IF NOT EXISTS smsgs(id INTEGER PRIMARY KEY, sid INT, uid INT, text TEXT, ts INT);
CREATE INDEX IF NOT EXISTS ix_smsgs ON smsgs(sid,id);
CREATE TABLE IF NOT EXISTS web_sessions(token TEXT PRIMARY KEY, uid INT, created REAL);
CREATE TABLE IF NOT EXISTS accounts(login TEXT PRIMARY KEY, uid INT UNIQUE, pwd TEXT, created REAL);
""")

def addcol(table, col):
    try: db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    except sqlite3.OperationalError: pass

for c in ("title TEXT", "photo TEXT", "rating REAL"): addcol("notes", c)
for c in ("code TEXT", "nick TEXT", "avatar TEXT", "vis TEXT DEFAULT 'friends'", "show_online INT DEFAULT 1",
          "seen INT DEFAULT 0", "lang TEXT DEFAULT 'ru'"): addcol("users", c)
for c in ("title_en TEXT", "genres_en TEXT"): addcol("items", c)
addcol("sessions", "token TEXT")   # ссылка-приглашение
addcol("smsgs", "img TEXT")         # фото в чате сессии (jpeg data-url)
db.executescript("""
CREATE INDEX IF NOT EXISTS ix_msg ON messages(a,b,id);
CREATE INDEX IF NOT EXISTS ix_msg_unread ON messages(b,a,rd);
CREATE INDEX IF NOT EXISTS ix_notes_uid ON notes(uid,id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_code ON users(code);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sess_tok ON sessions(token);
""")
db.commit()

# ───────────────────────── Тексты бота ─────────────────────────
BT = {
    "ru": {
        "open": "🎬 Открыть MY CINEMA", "buff": "Киноман",
        "friends_ok": "Вы теперь друзья в MY CINEMA. Списки друг друга смотри в разделе «Друзья».",
        "friend_added": "{name} добавился к тебе в друзья в MY CINEMA",
        "hello": "Привет! Это твой личный киноархив: смотри, оценивай, веди дневник и выбирай фильм на вечер.\n\n"
                 "/export выгрузит дневник в CSV\n/remind off выключит напоминания, /remind on включит\n"
                 "/lang en — switch to English",
        "remind_on": "Напоминания включены", "remind_off": "Напоминания выключены", "empty": "Архив пока пуст",
        "reminder": "🎬 Как насчёт сегодня вечером: «{title}» ({year})? Ты давно хотел посмотреть.",
        "lang_set": "Язык: русский", "lang_usage": "Используй: /lang ru или /lang en",
        "sess_invite": "🎬 {name} зовёт тебя смотреть вместе в MY CINEMA. Открой приложение и прими приглашение.",
        "sess_accepted": "✅ {name} принял(а) приглашение. Сессия открыта: «Смотреть вместе» в приложении.",
        "sess_ok": "✅ Приглашение принято. Открывай приложение: сессия уже ждёт.",
        "sess_no": "Приглашение отклонено.",
        "sess_joined": "✅ Ты в сессии с {name}. Открывай приложение.",
        "sess_gone": "Эта ссылка-приглашение уже не действует.",
        "sess_self": "Это твоя собственная ссылка. Отправь её тому, с кем хочешь смотреть.",
        "sess_busy_me": "Ты уже участвуешь в активной сессии. Заверши её в приложении.",
        "sess_busy_friend": "У пригласившего уже идёт другая сессия.",
        "sess_state": "Приглашение уже недействительно.",
        "sess_chat": "💬 {name} (смотрите вместе): {text}",
        "btn_accept": "✅ Принять", "btn_decline": "✖ Отклонить",
        "watch_usage": "Использование:\n/watch anime <shikimori_id>\n/watch movie <tmdb_id>",
        "login_ask": "🔐 Вход на сайт MY CINEMA. Если это ты, нажми кнопку. Если ты ничего не запрашивал(а), просто проигнорируй сообщение.",
        "login_btn": "✅ Войти на сайт", "login_ok": "✅ Готово. Возвращайся на сайт: вход выполнен.",
        "login_bad": "Ссылка для входа устарела. Обнови страницу сайта и попробуй снова.",
        "watch_btn": "▶️ Смотреть", "watch_ready": "Плеер готов. Нажми кнопку — он откроется прямо в Telegram.",
        "watch_none": "😔 Плеер для этого тайтла сейчас недоступен. Попробуй позже или вставь свою ссылку в приложении.",
    },
    "en": {
        "open": "🎬 Open MY CINEMA", "buff": "Movie buff",
        "friends_ok": "You are now friends in MY CINEMA. Check each other's lists in the “Friends” section.",
        "friend_added": "{name} added you as a friend in MY CINEMA",
        "hello": "Hi! This is your personal movie archive: watch, rate, keep a diary and pick a movie for tonight.\n\n"
                 "/export exports your diary to CSV\n/remind off turns reminders off, /remind on turns them on\n"
                 "/lang ru — переключить на русский",
        "remind_on": "Reminders are on", "remind_off": "Reminders are off", "empty": "Your archive is empty",
        "reminder": "🎬 How about tonight: “{title}” ({year})? You have wanted to watch it for a while.",
        "lang_set": "Language: English", "lang_usage": "Use: /lang ru or /lang en",
        "sess_invite": "🎬 {name} invites you to watch together in MY CINEMA. Open the app and accept.",
        "sess_accepted": "✅ {name} accepted your invitation. The session is open: “Watch together” in the app.",
        "sess_ok": "✅ Invitation accepted. Open the app: the session is waiting.",
        "sess_no": "Invitation declined.",
        "sess_joined": "✅ You are in a session with {name}. Open the app.",
        "sess_gone": "This invitation link is no longer valid.",
        "sess_self": "This is your own link. Send it to whoever you want to watch with.",
        "sess_busy_me": "You are already in an active session. End it in the app first.",
        "sess_busy_friend": "The person who invited you is already in another session.",
        "sess_state": "This invitation is no longer valid.",
        "sess_chat": "💬 {name} (watching together): {text}",
        "btn_accept": "✅ Accept", "btn_decline": "✖ Decline",
        "watch_usage": "Usage:\n/watch anime <shikimori_id>\n/watch movie <tmdb_id>",
        "login_ask": "🔐 Sign in to the MY CINEMA website. If this is you, tap the button. If you did not request it, just ignore this message.",
        "login_btn": "✅ Sign in", "login_ok": "✅ Done. Go back to the website: you are signed in.",
        "login_bad": "This sign-in link has expired. Refresh the website and try again.",
        "watch_btn": "▶️ Watch", "watch_ready": "The player is ready. Tap the button — it opens right inside Telegram.",
        "watch_none": "😔 No player is available for this title right now. Try later or paste your own link in the app.",
    },
}
LANG = {}   # кэш языков пользователей

def lang_of(uid):
    if uid not in LANG:
        r = db.execute("SELECT lang FROM users WHERE uid=?", (uid,)).fetchone()
        LANG[uid] = (r and r[0]) or "ru"
    return LANG[uid]

def bt(uid, key, **f):
    return BT[lang_of(uid)][key].format(**f)

# ───────────────────────── Пользователи ─────────────────────────
def new_code():
    al = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        c = "".join(secrets.choice(al) for _ in range(8))
        if not db.execute("SELECT 1 FROM users WHERE code=?", (c,)).fetchone(): return c

def ensure_user(uid, name=""):
    db.execute("INSERT OR IGNORE INTO users(uid,name) VALUES(?,?)", (uid, name))
    if not db.execute("SELECT code FROM users WHERE uid=?", (uid,)).fetchone()[0]:
        db.execute("UPDATE users SET code=? WHERE uid=?", (new_code(), uid))
    db.commit()

for _r in db.execute("SELECT uid FROM users WHERE code IS NULL").fetchall():
    db.execute("UPDATE users SET code=? WHERE uid=?", (new_code(), _r["uid"]))
db.commit()

def brief(u):
    on = bool(u["show_online"]) and time.time() - (u["seen"] or 0) < 120
    return {"uid": u["uid"], "name": u["nick"] or u["name"] or "", "avatar": u["avatar"] or "🎬",
            "vis": u["vis"] or "friends", "online": on, "seen": (u["seen"] or None) if u["show_online"] else None}

def are_friends(a, b):
    return bool(db.execute("SELECT 1 FROM friends WHERE a=? AND b=?", (a, b)).fetchone())

KNOWN, SEEN = set(), {}   # чтобы не писать в БД на каждый запрос

WEB_TTL = 90 * 86400                      # сессия сайта живёт 90 дней

def auth(request):
    tok = request.headers.get("X-Token", "")
    if tok:                                   # вход с сайта
        r = db.execute("SELECT uid,created FROM web_sessions WHERE token=?", (tok,)).fetchone()
        if not r or time.time() - r["created"] > WEB_TTL: raise web.HTTPUnauthorized()
        uid = r["uid"]
        if uid not in KNOWN: ensure_user(uid); KNOWN.add(uid)
    else:                                     # вход из Telegram Mini App
        try:
            d = dict(parse_qsl(request.headers.get("X-Init", ""), keep_blank_values=True))
            h = d.pop("hash", "")
            chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
            key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h):
                raise web.HTTPUnauthorized()
            u = json.loads(d["user"])
        except (KeyError, ValueError):
            raise web.HTTPUnauthorized()
        uid = u["id"]
        if uid not in KNOWN:
            ensure_user(uid, u.get("first_name", "")); KNOWN.add(uid)
    now = int(time.time())
    if now - SEEN.get(uid, 0) >= 30:          # раньше: 2 коммита на КАЖДЫЙ запрос
        SEEN[uid] = now
        db.execute("UPDATE users SET seen=? WHERE uid=?", (now, uid)); db.commit()
    return uid

# ───────────────────────── TMDB: общая сессия + кэш ─────────────────────────
HTTP = None
CACHE = {}

async def tmdb(path, lang="ru", ttl=600, **p):
    key = (path, lang, tuple(sorted(p.items())))
    hit = CACHE.get(key)
    if hit and hit[0] > time.time(): return hit[1]
    params = {"api_key": TMDB, "language": "ru-RU" if lang == "ru" else "en-US", **p}
    try:
        async with HTTP.get(f"https://api.themoviedb.org/3/{path}", params=params) as r:
            data = await r.json(); ok = r.status == 200
    except Exception as e:
        print("tmdb error:", path, type(e).__name__)
        return {}
    if ok:
        CACHE[key] = (time.time() + ttl, data)
        if len(CACHE) > 600:
            now = time.time()
            for k in [k for k, v in CACHE.items() if v[0] < now]: del CACHE[k]
            if len(CACHE) > 600: CACHE.clear()
    return data

def card(x, t=None):
    t = t or x.get("media_type")
    return dict(tid=x["id"], type=t, title=x.get("title") or x.get("name"),
        year=(x.get("release_date") or x.get("first_air_date") or "")[:4],
        poster=x.get("poster_path") and "https://image.tmdb.org/t/p/w342" + x["poster_path"],
        overview=x.get("overview", ""), vote=round(x.get("vote_average") or 0, 1))

def loc(r, lang):
    """Подставляет название и жанры на нужном языке."""
    if lang == "en":
        r["title"] = r.get("title_en") or r["title"]
        r["genres"] = r.get("genres_en") or r["genres"]
    r.pop("title_en", None); r.pop("genres_en", None)
    return r

async def backfill_en(rows):
    """Старые записи сохранены только по-русски: один раз подтягиваем английские названия."""
    todo = [r for r in rows if not r.get("title_en")][:30]
    if not todo: return
    sem = asyncio.Semaphore(6)
    async def one(r):
        async with sem:
            d = await tmdb(f"{r['type']}/{r['tid']}", "en", 3600)
        if not d: return                       # сеть недоступна: попробуем в следующий раз
        if "id" in d:
            r["title_en"] = d.get("title") or d.get("name") or r["title"]
            r["genres_en"] = ", ".join(g["name"] for g in d.get("genres", [])) or r["genres"]
        else:                                  # 404: больше не пытаемся
            r["title_en"], r["genres_en"] = r["title"], r["genres"]
        db.execute("UPDATE items SET title_en=?,genres_en=? WHERE uid=? AND tid=? AND type=?",
                   (r["title_en"], r["genres_en"], r["uid"], r["tid"], r["type"]))
    await asyncio.gather(*(one(r) for r in todo))
    db.commit()

# ───────────────────────── API ─────────────────────────
async def search(request):
    uid = auth(request)
    r = await tmdb("search/multi", lang_of(uid), 300, query=request.query.get("q", "")[:100])
    return web.json_response([card(x) for x in r.get("results", []) if x.get("media_type") in ("movie", "tv")][:12])

async def trending(request):
    uid = auth(request)
    r = await tmdb("trending/all/week", lang_of(uid), 1800)
    return web.json_response([card(x) for x in r.get("results", []) if x.get("media_type") in ("movie", "tv")][:12])

async def items(request):
    uid = auth(request); lang = lang_of(uid)
    rows = [dict(r) for r in db.execute("SELECT * FROM items WHERE uid=?", (uid,))]
    if lang == "en": await backfill_en(rows)
    return web.json_response([loc(r, lang) for r in rows])

FIELDS = ("status", "rating", "fav", "date", "mood", "note", "season", "episode")

async def save(request):
    uid = auth(request); b = await request.json()
    tid, t = int(b["tid"]), b["type"]
    if t not in ("movie", "tv"): raise web.HTTPBadRequest()
    if not db.execute("SELECT 1 FROM items WHERE uid=? AND tid=? AND type=?", (uid, tid, t)).fetchone():
        ru, en = await asyncio.gather(tmdb(f"{t}/{tid}", "ru", 3600), tmdb(f"{t}/{tid}", "en", 3600))
        if "id" not in ru: return web.json_response({"error": "tmdb"}, status=502)
        rt = ru.get("runtime") or (ru.get("episode_run_time") or [0])[0] or 0
        c = card(ru, t); ce = card(en, t) if "id" in en else c
        db.execute("INSERT OR IGNORE INTO items(uid,tid,type,title,title_en,year,poster,genres,genres_en,runtime,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (uid, tid, t, c["title"], ce["title"], c["year"], c["poster"],
             ", ".join(g["name"] for g in ru.get("genres", [])),
             ", ".join(g["name"] for g in en.get("genres", [])) if "id" in en else None, rt, "want"))
    sets = {k: b[k] for k in FIELDS if k in b}
    if sets:   # один UPDATE вместо отдельного на каждое поле
        db.execute(f"UPDATE items SET {','.join(k + '=?' for k in sets)} WHERE uid=? AND tid=? AND type=?", (*sets.values(), uid, tid, t))
    if b.get("status") == "watched":
        db.execute("UPDATE items SET date=COALESCE(date,?) WHERE uid=? AND tid=? AND type=?", (str(date.today()), uid, tid, t))
    db.commit()
    return web.json_response({"ok": 1})

async def remove(request):
    uid = auth(request); b = await request.json()
    db.execute("DELETE FROM items WHERE uid=? AND tid=? AND type=?", (uid, b["tid"], b["type"])); db.commit()
    return web.json_response({"ok": 1})

async def rnd(request):
    uid = auth(request); q = request.query; lang = lang_of(uid)
    rows = [loc(dict(r), lang) for r in db.execute("SELECT * FROM items WHERE uid=? AND status='want'", (uid,))]
    if q.get("genre"): rows = [r for r in rows if q["genre"] in (r["genres"] or "")]
    if q.get("max"): rows = [r for r in rows if 0 < r["runtime"] <= int(q["max"])]
    if q.get("type"): rows = [r for r in rows if r["type"] == q["type"]]
    return web.json_response(random.choice(rows) if rows else None)

async def discover(request):
    """Подбор по фильтрам: тип, жанр, страна, годы, рейтинг, длительность, сортировка (TMDB discover)."""
    uid = auth(request); lang = lang_of(uid); q = request.query
    def num(k):
        try: return int(q[k])
        except (KeyError, ValueError): return None
    types = (q["type"],) if q.get("type") in ("movie", "tv") else ("movie", "tv")
    mx = num("max")
    if mx: types = ("movie",)                 # длительность осмысленна только для фильмов
    y1, y2, page = num("y1"), num("y2"), min(max(num("page") or 1, 1), 20)
    try: rv = min(max(float(q.get("rate") or 0), 0), 9.5)
    except ValueError: rv = 0
    sort = q.get("sort", "pop")
    country = "".join(ch for ch in q.get("country", "") if ch.isalpha())[:2].upper()
    want_genre = "gm" in q or "gt" in q
    today = str(date.today())
    tys, jobs = [], []
    for ty in types:
        gid = num("gm" if ty == "movie" else "gt")
        if want_genre and gid is None: continue      # у этого типа такого жанра нет
        dk = "primary_release_date" if ty == "movie" else "first_air_date"
        p = {"page": page, "include_adult": "false",
             "sort_by": {"rate": "vote_average.desc", "new": dk + ".desc"}.get(sort, "popularity.desc"),
             "vote_count.gte": 300 if sort == "rate" else 100 if rv else 15}
        lo = f"{y1}-01-01" if y1 and 1870 < y1 < 2100 else None
        hi = f"{y2}-12-31" if y2 and 1870 < y2 < 2100 else None
        if sort == "new": hi = min(hi, today) if hi else today     # без ещё не вышедших
        if lo: p[dk + ".gte"] = lo
        if hi: p[dk + ".lte"] = hi
        if rv: p["vote_average.gte"] = rv
        if gid: p["with_genres"] = gid
        if country: p["with_origin_country"] = country
        if mx: p["with_runtime.lte"], p["with_runtime.gte"] = mx, 40
        tys.append(ty); jobs.append(tmdb(f"discover/{ty}", lang, 600, **p))
    res = await asyncio.gather(*jobs)
    out = [(x, ty) for ty, d in zip(tys, res) for x in d.get("results", []) if x.get("poster_path") and x.get("id")]
    key = {"rate": lambda e: e[0].get("vote_average") or 0,
           "new": lambda e: e[0].get("release_date") or e[0].get("first_air_date") or ""}.get(sort, lambda e: e[0].get("popularity") or 0)
    out.sort(key=key, reverse=True)
    return web.json_response([card(x, ty) for x, ty in out])

async def recs(request):
    uid = auth(request); lang = lang_of(uid)
    mine = {(r["tid"], r["type"]) for r in db.execute("SELECT tid,type FROM items WHERE uid=?", (uid,))}
    seeds = db.execute("SELECT tid,type FROM items WHERE uid=? AND rating>=8 ORDER BY rating DESC, date DESC LIMIT 6", (uid,)).fetchall()
    res = await asyncio.gather(*(tmdb(f"{s['type']}/{s['tid']}/recommendations", lang, 1800) for s in seeds))  # параллельно
    score = {}
    for s, r in zip(seeds, res):
        for x in r.get("results", [])[:15]:
            k = (x["id"], s["type"])
            if k not in mine:
                e = score.setdefault(k, [0, card(x, s["type"])]); e[0] += 1 + (x.get("vote_average") or 0) / 10
    top = sorted(score.values(), key=lambda e: -e[0])[:12]
    return web.json_response({"seeds": len(seeds), "items": [c for _, c in top]})

# ───────────────────────── Бесплатный просмотр (легально) ─────────────────────────
AR = {}                                   # tid -> (до какого времени кэш, identifier или None)
AR_ID = re.compile(r"^[\w.\-]{1,100}$")

def _norm(s):
    return re.sub(r"[\W_]+", " ", str(s or "").lower().replace("ё", "е")).strip()

async def archive_find(t, tid):
    """Ищет фильм в Internet Archive (коллекция feature_films, в основном общественное достояние)."""
    if t != "movie": return None
    hit = AR.get(tid)
    if hit and hit[0] > time.time(): return hit[1]
    d = await tmdb(f"movie/{tid}", "en", 86400)
    title, yr = d.get("title") or "", (d.get("release_date") or "")[:4]
    if not title or not yr.isdigit(): return None
    names = {_norm(title), _norm(d.get("original_title"))} - {""}
    cols = "(feature_films OR silent_films OR SciFi_Horror OR film_noir OR comedy_films)"
    q = 'title:("%s") AND mediatype:movies AND collection:%s' % (title.replace('"', " "), cols)
    try:
        async with HTTP.get("https://archive.org/advancedsearch.php", params=[
                ("q", q), ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "year"), ("rows", "20"), ("output", "json")]) as r:
            docs = (await r.json(content_type=None))["response"]["docs"]
    except Exception as e:
        print("archive error:", type(e).__name__); return None      # ошибку не кэшируем
    found = None
    for x in docs:
        tt = x.get("title"); tt = tt[0] if isinstance(tt, list) and tt else tt
        ry = str(x.get("year") or "")[:4]
        nt = _norm(re.sub(r"\(\s*\d{4}\s*\)", "", str(tt or "")))      # «Название (1902)» -> «название»
        if nt and any(nt == n or nt.startswith(n + " ") for n in names) and ry.isdigit() \
                and abs(int(ry) - int(yr)) <= 1 and AR_ID.match(x.get("identifier", "")):
            found = x["identifier"]; break
    AR[tid] = (time.time() + 86400, found)
    if len(AR) > 2000: AR.clear()
    return found

RT = {}                                   # (type, tid) -> (до какого времени кэш, список видео)

RUTUBE_DEBUG = os.getenv("RUTUBE_DEBUG", "1") == "1"      # 1 = печатать в консоль сырой ответ Rutube и решение по каждому видео
# Доверенные каналы. Rutube в поиске НЕ отдаёт флаг «официальный», поэтому доверие задаём по имени канала.
# Свои каналы добавляй в .env: RUTUBE_TRUSTED_AUTHORS="Название канала,12345" (название или id автора, через запятую).
TRUSTED_WORDS = ("мосфильм", "ленфильм", "союзмультфильм", "киностудия", "киноконцерн", "кинокомпания",
                 "госфильмофонд", "стс", "премьер", "premier")                      # слова целиком, не части слов
RUTUBE_TRUSTED = {s.strip().lower() for s in os.getenv("RUTUBE_TRUSTED_AUTHORS", "").split(",") if s.strip()}
OFFICIAL_KEYS = ("is_official", "official", "is_verified", "verified", "is_official_channel")   # на случай, если флаг появится
BLOCK_KEYS = ("is_hidden", "is_adult", "is_livestream", "is_audio", "is_paid", "paid")

def rt_dbg(*a):
    if RUTUBE_DEBUG: print("[rutube]", *a, flush=True)

def trusted_author(a):
    """Канал доверенный: флаг от Rutube, id/название из RUTUBE_TRUSTED_AUTHORS или слово из TRUSTED_WORDS в названии."""
    if any(a.get(k) for k in OFFICIAL_KEYS): return "flag"
    nm = " " + _norm(a.get("name")) + " "
    for e in RUTUBE_TRUSTED:
        if str(a.get("id", "")).lower() == e or " " + _norm(e) + " " in nm: return "env"
    for w in TRUSTED_WORDS:
        if " " + w + " " in nm: return "word:" + w
    return ""

def title_ok(nt, names):
    """Название фильма входит в заголовок видео: целиком (по границам слов) либо все его слова по отдельности."""
    for n in names:
        if " " + n + " " in nt: return True
        toks = n.split()
        if len(toks) >= 2 and all(" " + tk + " " in nt for tk in toks): return True
    return False

async def rutube_find(title, year, media_type="movie", names=None):
    """Полные видео с доверенных каналов Rutube -> [{"kind","src","title","dur"}]. Пишет в консоль, что вернул Rutube и почему видео принято/отброшено."""
    names = names or ({_norm(title)} - {""})
    if not names: return []
    key = ("rt", media_type, tuple(sorted(names)), str(year)); hit = RT.get(key)
    if hit and hit[0] > time.time(): return hit[1]
    need = 40 * 60 if media_type == "movie" else 15 * 60   # фильм: от 40 минут, серия: от 15
    query = f"{title} {year}".strip()
    rt_dbg(f"search {media_type}: query={query!r} names={sorted(names)} min_duration={need}s trusted_env={sorted(RUTUBE_TRUSTED)}")
    try:
        async with HTTP.get("https://rutube.ru/api/search/video/", params={"query": query},
                            headers={"User-Agent": UA, "Accept": "application/json"}) as r:
            raw = await r.text()
            rt_dbg("GET", str(r.url), "-> HTTP", r.status, "bytes:", len(raw))
            rt_dbg("RAW (first 3000 chars):", raw[:3000])
            if r.status != 200: return []                          # ошибку не кэшируем
            data = json.loads(raw)
    except Exception as e:
        print("[rutube] error:", type(e).__name__, e, flush=True); return []
    res = data.get("results") or [] if isinstance(data, dict) else []
    out = []
    for idx, x in enumerate(res):
        a = x.get("author") or {}
        vid = str(x.get("id") or "")
        dur = x.get("duration") or 0
        nt = " " + _norm(x.get("title")) + " "
        trust = trusted_author(a)
        why = []
        if not RT_ID.match(vid): why.append("bad_id")
        if not trust: why.append("author_not_trusted")
        blocked = [k for k in BLOCK_KEYS if x.get(k)]
        if blocked: why.append("flags:" + ",".join(blocked))
        if not isinstance(dur, (int, float)) or dur < need: why.append(f"short({dur})")
        if not title_ok(nt, names): why.append("title_mismatch")
        if idx < 10:
            rt_dbg(f"#{idx} id={vid} title={x.get('title')!r} dur={dur} author={a.get('name')!r} author_id={a.get('id')} "
                   f"trust={trust or '-'} -> {'OK' if not why else 'skip: ' + '; '.join(why)}")
        if why: continue
        out.append({"kind": "rutube", "src": f"https://rutube.ru/play/embed/{vid}",
                    "title": str(x.get("title") or "")[:90], "dur": int(dur // 60)})
        if len(out) >= 6: break
    rt_dbg(f"accepted: {len(out)}")
    RT[key] = (time.time() + (21600 if out else 300), out)       # пустой результат помним лишь 5 минут
    if len(RT) > 2000: RT.clear()
    return out

VK_TOKEN = os.getenv("VK_TOKEN", "")      # токен пользователя VK с доступом к видео (в .env: VK_TOKEN=...)
VK_PLAYER = re.compile(r"^https://(?:vk\.com|vkvideo\.ru)/video_ext\.php\?")

async def vk_find(title, year, media_type="movie", names=None):
    """Полные видео с доверенных сообществ VK Видео (video.search) -> [{"kind","src","title","dur"}]."""
    names = names or ({_norm(title)} - {""})
    if not names: return []
    if not VK_TOKEN:
        rt_dbg("vk: VK_TOKEN не задан, пропускаю"); return []
    key = ("vk", media_type, tuple(sorted(names)), str(year)); hit = RT.get(key)
    if hit and hit[0] > time.time(): return hit[1]
    need = 2400 if media_type == "movie" else 900
    query = f"{title} {year}".strip()
    try:
        async with HTTP.get("https://api.vk.com/method/video.search", params={
                "access_token": VK_TOKEN, "v": "5.199", "q": query, "count": "30", "sort": "2",
                "filters": "long", "adult": "0", "extended": "1"}) as r:
            raw = await r.text()
            rt_dbg("vk GET video.search -> HTTP", r.status, "bytes:", len(raw))
            rt_dbg("vk RAW (first 3000 chars):", raw[:3000])
            data = json.loads(raw)
    except Exception as e:
        print("[vk] error:", type(e).__name__, e, flush=True); return []
    if not isinstance(data, dict) or "error" in data:
        print("[vk] API error:", (data.get("error") if isinstance(data, dict) else data), flush=True); return []
    resp = data.get("response") or {}
    groups = {g.get("id"): g for g in resp.get("groups") or []}
    out = []
    for idx, x in enumerate(resp.get("items") or []):
        oid = x.get("owner_id") or 0
        g = groups.get(-oid) if oid < 0 else None                  # владелец-сообщество (свои пользователи не доверенные)
        a = {"name": (g or {}).get("name"), "id": oid, "verified": (g or {}).get("verified")} if g else {}
        trust = trusted_author(a) if a else ""
        src = str(x.get("player") or "")
        dur = x.get("duration") or 0
        why = []
        if not trust: why.append("author_not_trusted")
        if not VK_PLAYER.match(src): why.append("no_player")
        if not isinstance(dur, (int, float)) or dur < need: why.append(f"short({dur})")
        if not title_ok(" " + _norm(x.get("title")) + " ", names): why.append("title_mismatch")
        if idx < 10:
            rt_dbg(f"vk #{idx} {oid}_{x.get('id')} title={x.get('title')!r} dur={dur} owner={(g or {}).get('name')!r} "
                   f"trust={trust or '-'} -> {'OK' if not why else 'skip: ' + '; '.join(why)}")
        if why: continue
        out.append({"kind": "vk", "src": src + ("" if "hd=" in src else "&hd=2"),
                    "title": str(x.get("title") or "")[:90], "dur": int(dur // 60)})
        if len(out) >= 6: break
    rt_dbg(f"vk accepted: {len(out)}")
    RT[key] = (time.time() + (21600 if out else 300), out)
    return out

async def archive_player(tid):
    ident = await archive_find("movie", tid)
    return [{"kind": "archive", "src": f"https://archive.org/embed/{ident}", "title": "Internet Archive", "dur": 0}] if ident else []

async def get_movie_players(title, year, media_type, shikimori_id=None, tmdb_id=None, names=None, fast=False):
    """Единый менеджер: собирает плееры из всех легальных источников. Пустой список = «Пока недоступно / Вставь свою ссылку».
    media_type: 'movie' | 'tv' | 'anime'. Аниме (или есть shikimori_id) ищется теми же источниками, что и сериалы."""
    mt = "tv" if (media_type == "anime" or shikimori_id) else media_type
    jobs = [] if fast else [rutube_find(title, year, mt, names), vk_find(title, year, mt, names)]
    async def _bal():   # Kodik (аниме по Shikimori) + Kodik/Collaps для фильмов по TMDB -> iframe-плееры
        imdb = None
        if tmdb_id:
            try:
                ext = await tmdb(f"{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}/external_ids", "en", 86400)
                imdb = ext.get("imdb_id") or None
            except Exception as e:
                log.warning("external_ids tmdb=%s: %s: %s", tmdb_id, type(e).__name__, e)
        res = []
        if shikimori_id:
            u = await get_anime_player(str(shikimori_id), HTTP)
            log.info("[players] anime shiki=%s -> %s", shikimori_id, u)
            if u: res.append({"kind": "kodik", "src": u, "title": "Kodik", "dur": 0})
        if tmdb_id:
            u = await get_movie_balancer(str(tmdb_id), HTTP, imdb_id=imdb)
            log.info("[players] movie tmdb=%s imdb=%s -> %s", tmdb_id, imdb, u)
            if u:
                kind = "kodik" if "kodik" in urlparse(u).netloc else "collaps"
                res.append({"kind": kind, "src": u, "title": kind.capitalize(), "dur": 0})
        return res
    jobs.append(_bal())
    if mt == "movie" and tmdb_id and not fast: jobs.append(archive_player(tmdb_id))
    out, seen = [], set()
    # у каждого источника свой потолок по времени: один тормозящий сервис не задерживает остальные
    for r in await asyncio.gather(*(asyncio.wait_for(j, 7) for j in jobs), return_exceptions=True):   # сбой одного источника не ломает остальные
        if isinstance(r, Exception):
            log.warning("[players] source failed: %s %s", type(r).__name__, r); continue
        for p in r:
            if p["src"] not in seen: seen.add(p["src"]); out.append(p)
    order = ("kodik", "collaps", "archive", "rutube", "vk")
    out.sort(key=lambda p: order.index(p["kind"]))
    return out[:8]

async def info(request):
    """Трейлер (YouTube) и где смотреть легально (данные TMDB/JustWatch)."""
    uid = auth(request); lang = lang_of(uid)
    t = request.query.get("type"); tid = int(request.query["tid"]); reg = request.query.get("region", "EE")[:2].upper()
    if t not in ("movie", "tv"): raise web.HTTPBadRequest()
    v, w = await asyncio.gather(
        tmdb(f"{t}/{tid}/videos", lang, 3600, include_video_language="ru,en" if lang == "ru" else "en"),
        tmdb(f"{t}/{tid}/watch/providers", lang, 3600))
    vids = [x for x in v.get("results", []) if x.get("site") == "YouTube" and x.get("type") in ("Trailer", "Teaser")]
    vids.sort(key=lambda x: (x["type"] != "Trailer", not x.get("official"), x.get("iso_639_1") != lang))
    r = (w.get("results") or {}).get(reg) or {}
    prov = lambda k: [{"name": p["provider_name"], "logo": "https://image.tmdb.org/t/p/w92" + p["logo_path"]}
                      for p in r.get(k, []) if p.get("logo_path")]
    free, seen_n = [], set()
    for p in prov("free") + prov("ads"):                      # бесплатно и бесплатно с рекламой
        if p["name"] not in seen_n: seen_n.add(p["name"]); free.append(p)
    return web.json_response({"trailer": vids[0]["key"] if vids else None, "link": r.get("link"), "free": free})

PL_CACHE = {}

async def players_api(request):
    """Плееры отдельно от info, чтобы карточка открывалась сразу. fast=1 — только Kodik/Collaps (для мгновенного запуска)."""
    auth(request)
    t = request.query.get("type"); tid = int(request.query["tid"]); fast = request.query.get("fast") == "1"
    if t not in ("movie", "tv"): raise web.HTTPBadRequest()
    shiki = request.query.get("shikimori_id") or None
    ck = (t, tid, shiki, fast)
    hit = PL_CACHE.get(ck)
    if hit and hit[0] > time.time():
        return web.json_response({"players": hit[1]})
    d = await tmdb(f"{t}/{tid}", "ru", 86400)
    title = d.get("title") or d.get("name") or ""
    year = (d.get("release_date") or d.get("first_air_date") or "")[:4]
    names = {_norm(title), _norm(d.get("original_title") or d.get("original_name"))} - {""}
    if not shiki and d.get("original_language") == "ja" and any(g.get("id") == 16 for g in d.get("genres", [])):
        try:   # японская анимация -> Shikimori ID для Kodik (не дольше 4 секунд)
            shiki = await asyncio.wait_for(
                find_shikimori_id([d.get("original_title") or d.get("original_name"), title], year, HTTP), 4)
        except Exception as e:
            log.warning("shikimori lookup tid=%s: %s: %s", tid, type(e).__name__, e)
    try:
        players = await asyncio.wait_for(
            get_movie_players(title, year, t, tmdb_id=tid, names=names, shikimori_id=shiki, fast=fast), 12)
    except Exception as e:
        log.warning("players tid=%s: %s: %s", tid, type(e).__name__, e)
        players = []
    if len(PL_CACHE) > 500: PL_CACHE.clear()
    PL_CACHE[ck] = (time.time() + (600 if players else 45), players)
    return web.json_response({"players": players})

# ───────────────────────── Онлайн-кинотеатры: прямые ссылки на страницу фильма ─────────────────────────
# Данные берём у api.kinopoisk.dev (поле watchability: сервис, логотип и ссылка именно на этот фильм).
# Токен: KINOPOISK_TOKEN в .env (бесплатный, выдаёт @kinopoiskdev_bot).
KP_TOKEN = os.getenv("KINOPOISK_TOKEN", "").strip()
KP_API = "https://api.kinopoisk.dev"
KP_FIELDS = ("id", "name", "alternativeName", "year", "type", "externalId", "watchability")
KP_TV = {"tv-series", "animated-series", "anime"}
CIN_CACHE = {}

async def kp_get(path, params):
    try:
        async with HTTP.get(KP_API + path, params=params, headers={"X-API-KEY": KP_TOKEN},
                            timeout=aiohttp.ClientTimeout(total=6)) as r:
            if r.status != 200:
                log.warning("[cinemas] kinopoisk.dev %s -> HTTP %s", path, r.status)
                return None
            return await r.json()
    except Exception as e:
        log.warning("[cinemas] kinopoisk.dev %s: %s: %s", path, type(e).__name__, e)
        return None

def kp_pick(docs, year, is_tv, names, need_name):
    """Выбирает подходящий тайтл: год ±1 (защита от совпадения TMDB-id у фильма и сериала), тип, название."""
    best, best_s = None, 0
    for d in docs or []:
        s = 1
        try: dy, y = int(d.get("year")), int(year)
        except (TypeError, ValueError): dy = y = None
        if dy is not None:
            if abs(dy - y) > 1: continue
            s += 2 - abs(dy - y)
        if (d.get("type") in KP_TV) == is_tv: s += 2
        if names & {_norm(d.get("name")), _norm(d.get("alternativeName"))}: s += 3
        elif need_name: continue
        if s > best_s: best, best_s = d, s
    return best

async def cinemas_api(request):
    """Онлайн-кинотеатры, где есть фильм/сериал, с прямыми ссылками. Отдельно от info, чтобы карточка открывалась сразу."""
    auth(request)
    t = request.query.get("type"); tid = int(request.query["tid"])
    if t not in ("movie", "tv"): raise web.HTTPBadRequest()
    if not KP_TOKEN:
        log.warning("[cinemas] KINOPOISK_TOKEN не задан в .env — кнопки кинотеатров недоступны")
        return web.json_response({"items": [], "error": "no_token"})
    ck = (t, tid); hit = CIN_CACHE.get(ck)
    if hit and hit[0] > time.time():
        return web.json_response({"items": hit[1]})
    d = await tmdb(f"{t}/{tid}", "ru", 86400, append_to_response="external_ids")
    title = d.get("title") or d.get("name") or ""
    orig = d.get("original_title") or d.get("original_name") or ""
    year = (d.get("release_date") or d.get("first_air_date") or "")[:4]
    imdb = d.get("imdb_id") or (d.get("external_ids") or {}).get("imdb_id")
    names, is_tv = {_norm(title), _norm(orig)} - {""}, t == "tv"
    sel = [("selectFields", f) for f in KP_FIELDS]
    doc = None
    for key, val in (("externalId.tmdb", tid), ("externalId.imdb", imdb)):
        if doc or not val: continue
        r = await kp_get("/v1.4/movie", sel + [(key, val), ("limit", 5)])
        doc = kp_pick((r or {}).get("docs"), year, is_tv, names, False)
    if not doc:   # в базе нет связки с TMDB/IMDb -> ищем по названию и году
        for q in (title, orig):
            if doc or not q: continue
            r = await kp_get("/v1.4/movie/search", [("query", q), ("limit", 10)])
            doc = kp_pick((r or {}).get("docs"), year, is_tv, names, True)
    if doc and not ((doc.get("watchability") or {}).get("items")):
        full = await kp_get(f"/v1.4/movie/{doc['id']}", [])
        if full: doc = full
    out, seen = [], set()
    for it in ((doc or {}).get("watchability") or {}).get("items") or []:
        name, url = str(it.get("name") or "").strip(), str(it.get("url") or "").strip()
        if not name or not url.startswith("https://") or name.lower() in seen: continue
        seen.add(name.lower())
        logo = str((it.get("logo") or {}).get("url") or "")
        out.append({"name": name, "url": url, "logo": logo if logo.startswith("https://") else ""})
    log.info("[cinemas] %s tid=%s kp=%s -> %s", t, tid, (doc or {}).get("id"), [o["name"] for o in out])
    if len(CIN_CACHE) > 500: CIN_CACHE.clear()
    CIN_CACHE[ck] = (time.time() + (6 * 3600 if out else 300), out)
    return web.json_response({"items": out})

async def embed(request):
    """Ссылка на Rutube / VK Видео -> данные для встроенного плеера."""
    auth(request); b = await request.json()
    raw = str(b.get("url") or "")
    m_ = URL_IN_TEXT.search(raw)
    url = (m_.group(0) if m_ else raw).strip().rstrip(".,;)")[:500]
    m = await resolve_media(url)
    if not m or m[0] not in ("rutube", "vk"): return web.json_response({"error": "unsupported"})
    return web.json_response({"kind": m[0], "vid": m[1]})

async def invite(request):
    uid = auth(request)
    return web.json_response({"link": f"https://t.me/{ME['u']}?start=f{uid}"})

async def friends(request):
    uid = auth(request)
    unread = {r[0]: r[1] for r in db.execute("SELECT a,COUNT(*) FROM messages WHERE b=? AND rd=0 GROUP BY a", (uid,))}  # без N+1
    out = [{**brief(u), "unread": unread.get(u["uid"], 0)}
           for u in db.execute("SELECT u.* FROM friends f JOIN users u ON u.uid=f.b WHERE f.a=?", (uid,))]
    return web.json_response(out)

async def friend_items(request):
    uid = auth(request); fid = int(request.query["fid"])
    t = db.execute("SELECT vis FROM users WHERE uid=?", (fid,)).fetchone()
    if not t: raise web.HTTPNotFound()
    vis = t["vis"] or "friends"
    if fid != uid and (vis == "hidden" or (vis == "friends" and not are_friends(uid, fid))):
        return web.json_response({"hidden": True}, status=403)
    lang = lang_of(uid)
    rows = db.execute("SELECT tid,type,title,title_en,year,poster,genres,genres_en,runtime,status,rating FROM items WHERE uid=?", (fid,))
    return web.json_response([loc(dict(r), lang) for r in rows])

async def feed(request):
    uid = auth(request); lang = lang_of(uid)
    seen = {(r["tid"], r["type"]) for r in db.execute("SELECT tid,type FROM items WHERE uid=? UNION SELECT tid,type FROM dismissed WHERE uid=?", (uid, uid))}
    jobs = [(r["type"], tmdb(f"{r['type']}/{r['tid']}/recommendations", lang, 1800))
            for r in db.execute("SELECT tid,type FROM items WHERE uid=? AND (rating>=8 OR fav=1) ORDER BY RANDOM() LIMIT 3", (uid,)).fetchall()]
    jobs += [(t, tmdb(f"discover/{t}", lang, sort_by="vote_count.desc", page=random.randint(1, 15), **{"vote_average.gte": 7}))
             for t in ("movie", "tv")]
    res = await asyncio.gather(*(j[1] for j in jobs))   # раньше: 5 запросов подряд, один за другим
    pool = {}
    for (t, _), d in zip(jobs, res):
        for x in d.get("results", []):
            k = (x["id"], t)
            if k not in seen and k not in pool and x.get("poster_path"): pool[k] = card(x, t)
    out = list(pool.values()); random.shuffle(out)
    return web.json_response(out[:20])

async def dismiss(request):
    uid = auth(request); b = await request.json()
    db.execute("INSERT OR IGNORE INTO dismissed VALUES(?,?,?)", (uid, b["tid"], b["type"])); db.commit()
    return web.json_response({"ok": 1})

async def settings(request):
    uid = auth(request)
    if request.method == "POST":
        b = await request.json(); db.execute("UPDATE users SET remind=? WHERE uid=?", (int(b["remind"]), uid)); db.commit()
    return web.json_response({"remind": db.execute("SELECT remind FROM users WHERE uid=?", (uid,)).fetchone()[0]})

async def notes(request):
    uid = auth(request)
    return web.json_response([dict(r) for r in db.execute("SELECT * FROM notes WHERE uid=? ORDER BY id DESC", (uid,))])

async def note_save(request):
    uid = auth(request); b = await request.json()
    t, ph, r, tx = b.get("title", ""), b.get("photo"), b.get("rating"), b.get("text", "")
    if b.get("id"):
        db.execute("UPDATE notes SET text=?,title=?,rating=? WHERE id=? AND uid=?", (tx, t, r, b["id"], uid))
        if ph is not None: db.execute("UPDATE notes SET photo=? WHERE id=? AND uid=?", (ph, b["id"], uid))
    else:
        db.execute("INSERT INTO notes(uid,text,created,title,photo,rating) VALUES(?,?,?,?,?,?)", (uid, tx, str(date.today()), t, ph or "", r))
    db.commit(); return web.json_response({"ok": 1})

async def note_del(request):
    uid = auth(request); b = await request.json()
    db.execute("DELETE FROM notes WHERE id=? AND uid=?", (b["id"], uid)); db.commit()
    return web.json_response({"ok": 1})

async def me(request):
    uid = auth(request)
    d = dict(db.execute("SELECT name,nick,avatar,vis,show_online,remind,code,lang FROM users WHERE uid=?", (uid,)).fetchone())
    d["vis"] = d["vis"] or "friends"; d["avatar"] = d["avatar"] or "🎬"; d["lang"] = d["lang"] or "ru"
    return web.json_response(d)

async def me_set(request):
    uid = auth(request); b = await request.json()
    for k in ("nick", "avatar", "vis", "show_online", "remind", "lang"):
        if k not in b: continue
        v = b[k]
        if k == "nick": v = str(v).strip()[:24]
        elif k == "avatar": v = str(v)[:8]
        elif k == "vis":
            if v not in ("public", "friends", "hidden"): continue
        elif k == "lang":
            if v not in ("ru", "en"): continue
            LANG[uid] = v
        else: v = 1 if v else 0
        db.execute(f"UPDATE users SET {k}=? WHERE uid=?", (v, uid))
    db.commit()
    return web.json_response({"ok": 1})

async def ping(request):
    auth(request)
    return web.json_response({"ok": 1})

async def lookup(request):
    uid = auth(request)
    code = "".join(ch for ch in request.query.get("code", "").upper() if ch.isalnum())
    t = db.execute("SELECT * FROM users WHERE code=?", (code,)).fetchone() if code else None
    if not t: return web.json_response({"error": "not_found"})
    if t["uid"] == uid: return web.json_response({"error": "self"})
    return web.json_response({**brief(t), "friend": are_friends(uid, t["uid"])})

async def friend_add(request):
    uid = auth(request); b = await request.json()
    if b.get("uid"):
        t = db.execute("SELECT * FROM users WHERE uid=? AND vis='public'", (int(b["uid"]),)).fetchone()
    else:
        code = "".join(ch for ch in str(b.get("code", "")).upper() if ch.isalnum())
        t = db.execute("SELECT * FROM users WHERE code=?", (code,)).fetchone() if code else None
    if not t: return web.json_response({"error": "not_found"})
    if t["uid"] == uid: return web.json_response({"error": "self"})
    new = not are_friends(uid, t["uid"])
    db.executemany("INSERT OR IGNORE INTO friends VALUES(?,?)", [(uid, t["uid"]), (t["uid"], uid)]); db.commit()
    if new:
        who = brief(db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone())["name"] or BT[lang_of(t["uid"])]["buff"]
        try: await BOT.send_message(t["uid"], bt(t["uid"], "friend_added", name=who))
        except Exception: pass
    return web.json_response({"ok": 1, "friend": brief(t)})

async def friend_del(request):
    uid = auth(request); b = await request.json(); fid = int(b["uid"])
    db.execute("DELETE FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)", (uid, fid, fid, uid)); db.commit()
    return web.json_response({"ok": 1})

async def chat_get(request):
    uid = auth(request); fid = int(request.query["fid"]); after = int(request.query.get("after", 0))
    if not are_friends(uid, fid): return web.json_response({"error": "not friends"}, status=403)
    if after:
        rows = db.execute("SELECT id,a,text,ts FROM messages WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND id>? ORDER BY id", (uid, fid, fid, uid, after)).fetchall()
    else:
        rows = db.execute("SELECT * FROM (SELECT id,a,text,ts FROM messages WHERE (a=? AND b=?) OR (a=? AND b=?) ORDER BY id DESC LIMIT 100) ORDER BY id", (uid, fid, fid, uid)).fetchall()
    # опрос идёт каждые 3 сек: пишем в БД только если есть что отмечать (раньше UPDATE + commit каждый раз)
    if db.execute("SELECT 1 FROM messages WHERE a=? AND b=? AND rd=0 LIMIT 1", (fid, uid)).fetchone():
        db.execute("UPDATE messages SET rd=1 WHERE a=? AND b=? AND rd=0", (fid, uid)); db.commit()
    f = db.execute("SELECT * FROM users WHERE uid=?", (fid,)).fetchone()
    return web.json_response({"msgs": [{"id": r["id"], "me": r["a"] == uid, "text": r["text"], "ts": r["ts"]} for r in rows], "friend": brief(f)})

async def chat_send(request):
    uid = auth(request); b = await request.json(); fid = int(b["fid"])
    text = str(b.get("text", "")).strip()[:1000]
    if not are_friends(uid, fid): return web.json_response({"error": "not friends"}, status=403)
    if not text: return web.json_response({"ok": 0})
    prev = db.execute("SELECT COUNT(*) FROM messages WHERE a=? AND b=? AND rd=0", (uid, fid)).fetchone()[0]
    cur = db.execute("INSERT INTO messages(a,b,text,ts) VALUES(?,?,?,?)", (uid, fid, text, int(time.time()))); db.commit()
    to = db.execute("SELECT * FROM users WHERE uid=?", (fid,)).fetchone()
    if prev == 0 and time.time() - (to["seen"] or 0) > 180:
        name = brief(db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone())["name"] or BT[lang_of(fid)]["buff"]
        try: await BOT.send_message(fid, f"💬 {name}: {text[:200]}", reply_markup=kb(fid))
        except Exception: pass
    return web.json_response({"ok": 1, "id": cur.lastrowid})

# ───────────────────────── Совместный просмотр ─────────────────────────
YT_ID = re.compile(r"^[\w-]{11}$")
VM_ID, VM_HASH = re.compile(r"^\d{5,12}$"), re.compile(r"^[0-9a-f]{6,16}$")
RT_ID, RT_KEY = re.compile(r"^[0-9a-f]{32}$"), re.compile(r"^[\w-]{1,64}$")
VK_HOSTS = ("vk.com", "vk.ru", "vkvideo.ru", "vkvideo.com")
VK_PATH = re.compile(r"(?:video|clip)(-?\d{1,12})_(\d{1,12})(?:_([0-9a-f]{6,24}))?", re.I)
VK_HASH = re.compile(r"^[0-9a-f]{6,24}$", re.I)
VIDEO_EXT = re.compile(r"\.(mp4|webm|ogv|m4v|mov|mkv|m3u8)$", re.I)
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.I)
REACT = {"👍", "😂", "😮", "❤️", "🔥"}
SESS_ERR = {"state": "sess_state", "busy_me": "sess_busy_me", "busy_friend": "sess_busy_friend",
            "gone": "sess_gone", "self": "sess_self"}
SSEEN, SWRITE = {}, {}    # когда участник последний раз опрашивал сессию (в памяти: не пишем в БД каждую секунду)
STYP = {}   # когда участник последний раз печатал в чате сессии
SCHAT, SNOTE, CTIME = {}, {}, {}   # id последнего сообщения чата / когда пушили в Telegram / антиспам
LASTCLEAN = [0.0]
WEB = None

# ── Разбор ссылок ──
def norm_file(url):
    # приложение открыто по https: видео по http браузер всё равно заблокирует
    return "https://" + url[7:] if url[:7].lower() == "http://" else url

def parse_media(url):
    """По самой ссылке, без сети: ('yt'|'vimeo'|'rutube'|'file', id или url) либо None."""
    url = (url or "").strip()[:500]
    try: u = urlparse(url)
    except ValueError: return None
    if u.scheme not in ("http", "https") or not u.hostname: return None
    host = u.hostname.lower()
    for pre in ("www.", "m."):
        if host.startswith(pre): host = host[len(pre):]
    p, q = [x for x in u.path.split("/") if x], dict(parse_qsl(u.query))
    vid = None
    if host == "youtu.be" and p: vid = p[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com") and p:
        if p[0] == "watch": vid = q.get("v")
        elif p[0] in ("shorts", "embed", "live", "v") and len(p) > 1: vid = p[1]
    if vid: return ("yt", vid) if YT_ID.match(vid) else None
    if host in ("vimeo.com", "player.vimeo.com"):
        i = p.index("video") + 1 if "video" in p[:-1] else next((n for n, x in enumerate(p) if VM_ID.match(x)), None)
        if i is None or i >= len(p) or not VM_ID.match(p[i]): return None
        h = q.get("h") or (p[i + 1] if len(p) > i + 1 else "")      # закрытые ролики: id:hash
        return ("vimeo", p[i] + (":" + h if VM_HASH.match(h) else ""))
    if host in VK_HOSTS or host.endswith(tuple("." + x for x in VK_HOSTS)):
        # vk.com/video-1_2, vkvideo.ru/video-1_2, vk.com/clip-1_2, vk.com/video?z=video-1_2%2F..., video_ext.php?oid=&id=&hash=
        if p and p[0] == "video_ext.php":
            o, i, hh = q.get("oid", ""), q.get("id", ""), q.get("hash", "")
            if re.fullmatch(r"-?\d{1,12}", o) and i.isdigit():
                return ("vk", f"{o}_{i}" + (":" + hh if VK_HASH.match(hh) else ""))
            return None
        m = next((m for m in (VK_PATH.search(x) for x in [u.path, q.get("z", ""), q.get("w", "")]) if m), None)
        if not m: return None
        hh = m.group(3) or ""
        return ("vk", f"{m.group(1)}_{m.group(2)}" + (":" + hh if hh else ""))
    if host == "rutube.ru":
        i = next((n for n, x in enumerate(p) if RT_ID.match(x)), None)
        if i is None: return None
        k = q.get("p", "")
        return ("rutube", p[i] + ("?p=" + k if RT_KEY.match(k) else ""))
    if (host == "dropbox.com" or host.endswith(".dropbox.com")) and VIDEO_EXT.search(u.path):
        q.pop("dl", None); q["raw"] = "1"                               # страница Dropbox → прямая выдача файла
        return ("file", norm_file(u._replace(query=urlencode(q)).geturl()))
    if VIDEO_EXT.search(u.path): return ("file", norm_file(url))
    return None

def _public_ip(ip):
    try: a = ipaddress.ip_address(ip)
    except ValueError: return False
    if a.version == 6 and a.ipv4_mapped: a = a.ipv4_mapped
    return a.is_global and not a.is_multicast

def host_ok(host):
    """Сервер не должен ходить во внутреннюю сеть (роутер, ngrok на :4040 и т.д.)."""
    h = (host or "").strip("[]").lower()
    try: ipaddress.ip_address(h)
    except ValueError: return bool(h) and h != "localhost" and not h.endswith((".local", ".internal", ".localhost"))
    return _public_ip(h)

class SafeResolver(aiohttp.abc.AbstractResolver):
    """Проверяет адрес в момент подключения: защита и от подмены DNS (rebinding)."""
    def __init__(self): self._r = aiohttp.ThreadedResolver()
    async def resolve(self, host, port=0, family=socket.AF_INET):
        ok = [r for r in await self._r.resolve(host, port, family) if _public_ip(r["host"])]
        if not ok: raise OSError("blocked address")
        return ok
    async def close(self): await self._r.close()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
VIDEO_CT = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl")

async def fetch_page(url, hops=4):
    """('direct', url) если ссылка сама отдаёт видео; ('html', url, text) для страницы; иначе None."""
    for _ in range(hops + 1):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not host_ok(u.hostname): return None
        async with WEB.get(url, allow_redirects=False, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8",
                                                                 "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}) as r:
            if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"]); continue
            if r.status >= 400: return None
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct.startswith("video/") or ct in VIDEO_CT: return ("direct", url)
            if ct and "html" not in ct and ct not in ("text/plain",): return None
            buf = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                buf += chunk
                if len(buf) > 1_500_000: break
            return ("html", url, bytes(buf).decode(r.charset or "utf-8", "ignore"))
    return None

def _attr(tag, name):
    m = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % name, tag, re.I)
    return (m.group(1) or m.group(2) or m.group(3)) if m else None

def extract_media(html, base):
    """Ищет видео на странице: og:video, <video>/<source>, <iframe> с YouTube/Vimeo/Rutube, JSON-LD, прямые ссылки."""
    cands = []
    def add(u):
        if not u: return
        u = unescape(u.strip()).replace("\\/", "/").replace("\\u0026", "&")
        cands.append(urljoin(base, u))
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        k = (_attr(tag, "property") or _attr(tag, "name") or "").lower()
        if k in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream", "twitter:player"):
            add(_attr(tag, "content"))
    for tag in re.findall(r"<(?:video|source)\b[^>]*>", html, re.I): add(_attr(tag, "src"))
    for tag in re.findall(r"<iframe\b[^>]*>", html, re.I): add(_attr(tag, "src"))
    for m in re.finditer(r'"(?:contentUrl|embedUrl|file|src|url|hls|hlsUrl|mp4)"\s*:\s*"([^"]+)"', html): add(m.group(1))
    for m in re.finditer(r"""https?:(?:\\?/){2}[^\s"'<>\\]+?\.(?:mp4|webm|m3u8|mov|m4v)(?:\?[^\s"'<>\\]*)?""", html): add(m.group(0))
    seen = set()
    for u in cands:
        if u in seen: continue
        seen.add(u)
        m = parse_media(u)
        if m: return m
    return None

VK_EMBED_HASH = re.compile(r"""video_ext\.php\?[^"'\s<>]*?hash=([0-9a-f]{6,24})|["']embed_hash["']\s*:\s*["']([0-9a-f]{6,24})["']""", re.I)

async def vk_add_hash(vid):
    """Для встраивания VK нужен hash: если его нет в ссылке, пробуем достать со страницы ролика (для открытых видео)."""
    oid, i = vid.split("_", 1)
    for host in ("vk.com", "vkvideo.ru"):
        try: page = await fetch_page(f"https://{host}/video{oid}_{i}")
        except Exception as e:
            print("vk fetch:", type(e).__name__); continue
        if not page or page[0] != "html": continue
        m = VK_EMBED_HASH.search(unescape(page[2]).replace("\\/", "/").replace("&amp;", "&"))
        if m: return vid + ":" + (m.group(1) or m.group(2))
    return vid

async def resolve_media(url):
    m = parse_media(url)
    if m and m[0] == "vk" and ":" not in m[1]:
        return ("vk", await vk_add_hash(m[1]))
    if m: return m
    try: page = await fetch_page(url)
    except Exception as e:
        print("media fetch:", type(e).__name__); return None
    if not page: return None
    if page[0] == "direct": return ("file", norm_file(page[1]))
    return extract_media(page[2], page[1])

# ── Вспомогательное ──
def urow(uid): return db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
def ubrief(uid):
    r = urow(uid)
    return brief(r) if r else None
def who(uid, viewer):
    b = ubrief(uid)
    return (b and b["name"]) or BT[lang_of(viewer)]["buff"]
def jerr(code): return web.json_response({"error": code})

def mine_session(uid, sid):
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not s or uid not in (s["host"], s["guest"]):
        raise web.HTTPNotFound(text='{"error":"gone"}', content_type="application/json")
    return s

def busy(uid):
    return bool(db.execute("SELECT 1 FROM sessions WHERE status='active' AND (host=? OR guest=?)", (uid, uid)).fetchone())

def bump(sid, **kw):
    cols = "".join(f"{k}=?," for k in kw)
    db.execute(f"UPDATE sessions SET {cols}ver=ver+1 WHERE id=?", (*kw.values(), sid)); db.commit()

def sweep():
    now = time.time()
    n = db.execute("UPDATE sessions SET status='ended',ver=ver+1 WHERE status IN ('invited','open') AND created<?", (now - 86400,)).rowcount
    n += db.execute("UPDATE sessions SET status='ended',ver=ver+1 WHERE status='active' AND MAX(hseen,gseen,created)<?", (now - 7200,)).rowcount
    if now - LASTCLEAN[0] > 60:    # чат живёт, пока жива сессия
        LASTCLEAN[0] = now
        n += db.execute("DELETE FROM smsgs WHERE sid IN (SELECT id FROM sessions WHERE status='ended')").rowcount
    if n: db.commit()

def touch(s, uid):
    now, k = time.time(), (s["id"], uid)
    SSEEN[k] = now
    if now - SWRITE.get(k, 0) >= 30:
        SWRITE[k] = now
        col = "hseen" if uid == s["host"] else "gseen"
        db.execute(f"UPDATE sessions SET {col}=? WHERE id=?", (now, s["id"])); db.commit()

def chat_last(sid):
    if sid not in SCHAT:
        SCHAT[sid] = db.execute("SELECT MAX(id) FROM smsgs WHERE sid=?", (sid,)).fetchone()[0] or 0
    return SCHAT[sid]

def sess_kb(uid, sid):
    L = lang_of(uid)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=BT[L]["btn_accept"], callback_data=f"sa:{sid}"),
         InlineKeyboardButton(text=BT[L]["btn_decline"], callback_data=f"sd:{sid}")],
        [InlineKeyboardButton(text=BT[L]["open"], web_app=WebAppInfo(url=URL))]])

# ── Принятие приглашений (из приложения, из кнопки в чате бота, по ссылке) ──
async def accept_session(uid, sid):
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not s or s["guest"] != uid or s["status"] != "invited": return "state"
    if busy(uid): return "busy_me"
    if busy(s["host"]): return "busy_friend"
    bump(sid, status="active", gseen=time.time())
    try: await BOT.send_message(s["host"], bt(s["host"], "sess_accepted", name=who(uid, s["host"])), reply_markup=kb(s["host"]))
    except Exception: pass
    return "ok"

async def join_by_token(uid, token):
    s = db.execute("SELECT * FROM sessions WHERE token=?", (token[:64],)).fetchone()
    if not s or s["status"] != "open": return "gone", None
    if s["host"] == uid: return "self", None
    if busy(uid): return "busy_me", None
    if busy(s["host"]): return "busy_friend", None
    db.execute("UPDATE sessions SET guest=?,status='active',gseen=?,ver=ver+1 WHERE id=? AND status='open'", (uid, time.time(), s["id"])); db.commit()
    try: await BOT.send_message(s["host"], bt(s["host"], "sess_accepted", name=who(uid, s["host"])), reply_markup=kb(s["host"]))
    except Exception: pass
    return "ok", s["host"]

# ── API ──
async def s_invite(request):
    uid = auth(request); b = await request.json(); fid = int(b["fid"])
    if not are_friends(uid, fid): return jerr("not_friends")
    sweep()
    if busy(uid): return jerr("busy_me")
    if busy(fid): return jerr("busy_friend")
    old = db.execute("SELECT id FROM sessions WHERE status='invited' AND host=? AND guest=?", (uid, fid)).fetchone()
    if old: return web.json_response({"ok": 1, "id": old["id"]})
    cur = db.execute("INSERT INTO sessions(host,guest,status,created) VALUES(?,?,'invited',?)", (uid, fid, time.time())); db.commit()
    try: await BOT.send_message(fid, bt(fid, "sess_invite", name=who(uid, fid)), reply_markup=sess_kb(fid, cur.lastrowid))
    except Exception: pass
    return web.json_response({"ok": 1, "id": cur.lastrowid})

async def s_link(request):
    """Ссылка-приглашение: подходит и тем, кто ещё не в друзьях. Открывается в боте → сразу в сессии."""
    uid = auth(request); sweep()
    if busy(uid): return jerr("busy_me")
    r = db.execute("SELECT token FROM sessions WHERE status='open' AND host=?", (uid,)).fetchone()
    if r: tok = r["token"]
    else:
        tok = secrets.token_urlsafe(9)
        db.execute("INSERT INTO sessions(host,guest,status,created,token) VALUES(?,0,'open',?,?)", (uid, time.time(), tok)); db.commit()
    return web.json_response({"ok": 1, "link": f"https://t.me/{ME['u']}?start=w{tok}"})

async def s_pending(request):
    uid = auth(request); sweep()
    inv = [{"id": r["id"], "from": ubrief(r["host"])} for r in db.execute("SELECT id,host FROM sessions WHERE status='invited' AND guest=? ORDER BY id", (uid,))]
    sent = []
    for r in db.execute("SELECT id,guest,status,token FROM sessions WHERE status IN ('invited','open') AND host=? ORDER BY id", (uid,)):
        if r["status"] == "open": sent.append({"id": r["id"], "to": None, "link": f"https://t.me/{ME['u']}?start=w{r['token']}"})
        else: sent.append({"id": r["id"], "to": ubrief(r["guest"])})
    a = db.execute("SELECT * FROM sessions WHERE status='active' AND (host=? OR guest=?) LIMIT 1", (uid, uid)).fetchone()
    act = {"id": a["id"], "peer": ubrief(a["guest"] if a["host"] == uid else a["host"]),
           "fresh": max(a["hseen"] or 0, a["gseen"] or 0) > time.time() - 180} if a else None
    return web.json_response({"invites": inv, "sent": sent, "active": act})

async def s_respond(request):
    uid = auth(request); b = await request.json(); sid = int(b["id"]); s = mine_session(uid, sid)
    if not b.get("accept"):
        if s["guest"] != uid or s["status"] != "invited": return jerr("state")
        bump(sid, status="ended"); return web.json_response({"ok": 1})
    code = await accept_session(uid, sid)
    return web.json_response({"ok": 1}) if code == "ok" else jerr(code)

async def s_get(request):
    uid = auth(request); s = mine_session(uid, int(request.query["id"])); v = int(request.query.get("v", 0))
    host = s["host"] == uid; peer = s["guest"] if host else s["host"]
    if s["status"] == "active": touch(s, uid)
    base = {"now": time.time(), "ver": s["ver"], "status": s["status"], "chat": chat_last(s["id"]),
            "peer_on": time.time() - SSEEN.get((s["id"], peer), 0) < 6,
            "peer_typing": time.time() - STYP.get((s["id"], peer), 0) < 4}
    if v == s["ver"]: return web.json_response({**base, "same": 1})
    return web.json_response({**base, "id": s["id"], "role": "host" if host else "guest", "peer": ubrief(peer),
        "url": s["url"], "kind": s["kind"], "vid": s["vid"], "hr": s["hr"], "gr": s["gr"],
        "started": s["started"], "playing": s["playing"], "pos": s["pos"], "upd": s["upd"]})

async def s_media(request):
    uid = auth(request); b = await request.json(); sid = int(b["id"]); s = mine_session(uid, sid)
    if s["host"] != uid or s["status"] != "active": return jerr("state")
    raw = str(b.get("url") or "")
    m_ = URL_IN_TEXT.search(raw)                       # можно вставить хоть весь текст «смотри: https://…»
    url = (m_.group(0) if m_ else raw).strip().rstrip(".,;)")[:500]
    m = await resolve_media(url)
    if not m: return jerr("unsupported")
    s = mine_session(uid, sid)                          # пока искали видео, сессию могли завершить
    if s["status"] != "active": return jerr("state")
    # ведущий выбрал видео сам, поэтому «готов» у него сразу; гостю остаётся нажать одну кнопку
    bump(sid, url=url, kind=m[0], vid=m[1], hr=1, gr=0, started=0, playing=0, pos=0, upd=0)
    return web.json_response({"ok": 1})

async def s_ready(request):
    uid = auth(request); b = await request.json(); s = mine_session(uid, int(b["id"]))
    if s["status"] != "active": return jerr("state")
    if not s["kind"]: return jerr("nomedia")
    if s["started"]: return web.json_response({"ok": 1})
    host = s["host"] == uid; v = 1 if b.get("ready") else 0
    hr, gr = (v, s["gr"]) if host else (s["hr"], v)
    if hr and gr: bump(s["id"], hr=1, gr=1, started=1, playing=1, pos=0, upd=time.time())   # согласились оба: поехали
    else: bump(s["id"], **{"hr" if host else "gr": v})
    return web.json_response({"ok": 1})

async def s_ctl(request):
    """Управление воспроизведением: только ведущий. play / pause / seek / reset (вернуться в лобби)."""
    uid = auth(request); b = await request.json(); s = mine_session(uid, int(b["id"]))
    if s["host"] != uid or s["status"] != "active": return jerr("state")
    act = b.get("act")
    try: pos = float(b.get("pos", 0))
    except (TypeError, ValueError): return jerr("bad")
    if not 0 <= pos < 1e6: return jerr("bad")
    now = time.time()
    if act == "reset": bump(s["id"], started=0, playing=0, pos=0, hr=0, gr=0)
    elif not s["started"]: return jerr("state")
    elif act == "play": bump(s["id"], playing=1, pos=pos, upd=now)
    elif act == "pause": bump(s["id"], playing=0, pos=pos, upd=now)
    elif act == "seek": bump(s["id"], pos=pos, upd=now)      # флаг playing не трогаем
    else: return jerr("bad")
    return web.json_response({"ok": 1})

async def s_stop(request):
    uid = auth(request); b = await request.json(); s = mine_session(uid, int(b["id"]))
    if s["status"] in ("invited", "open", "active"): bump(s["id"], status="ended")
    return web.json_response({"ok": 1})

# ── Чат сессии ──
async def s_chat_get(request):
    uid = auth(request); s = mine_session(uid, int(request.query["id"])); after = int(request.query.get("after", 0))
    if after:
        rows = db.execute("SELECT id,uid,text,ts,(img IS NOT NULL AND img!='') AS hi FROM smsgs WHERE sid=? AND id>? ORDER BY id LIMIT 200", (s["id"], after)).fetchall()
    else:
        rows = db.execute("SELECT * FROM (SELECT id,uid,text,ts,(img IS NOT NULL AND img!='') AS hi FROM smsgs WHERE sid=? ORDER BY id DESC LIMIT 100) ORDER BY id", (s["id"],)).fetchall()
    return web.json_response({"msgs": [{"id": r["id"], "me": r["uid"] == uid, "text": r["text"], "ts": r["ts"], "img": bool(r["hi"])} for r in rows]})

async def s_chat_send(request):
    uid = auth(request); b = await request.json(); s = mine_session(uid, int(b["id"]))
    if s["status"] != "active": return jerr("state")
    text = str(b.get("text", "")).strip()[:500]
    img = b.get("img") or ""
    if not (isinstance(img, str) and img.startswith("data:image/jpeg;base64,") and len(img) <= 900_000): img = ""
    if not text and not img: return web.json_response({"ok": 0})
    now = time.time(); recent = [t for t in CTIME.get(uid, []) if now - t < 10]
    if len(recent) >= 8: return jerr("slow")
    CTIME[uid] = recent + [now]
    cur = db.execute("INSERT INTO smsgs(sid,uid,text,ts,img) VALUES(?,?,?,?,?)", (s["id"], uid, text, int(now), img)); db.commit()
    STYP.pop((s["id"], uid), None)
    SCHAT[s["id"]] = cur.lastrowid
    peer = s["guest"] if s["host"] == uid else s["host"]
    k = (s["id"], peer)
    # собеседник сейчас не на экране сессии: одно уведомление в Telegram не чаще раза в 2 минуты (эмодзи-реакции не считаем)
    if text not in REACT and now - SSEEN.get(k, 0) > 8 and now - SNOTE.get(k, 0) > 120:
        SNOTE[k] = now
        try: await BOT.send_message(peer, bt(peer, "sess_chat", name=who(uid, peer), text=(text or "📷")[:200]), reply_markup=kb(peer))
        except Exception: pass
    return web.json_response({"ok": 1, "id": cur.lastrowid})

async def s_chat_img(request):
    uid = auth(request); s = mine_session(uid, int(request.query["id"]))
    r = db.execute("SELECT img FROM smsgs WHERE id=? AND sid=?", (int(request.query["mid"]), s["id"])).fetchone()
    return web.json_response({"img": (r["img"] if r and r["img"] else "")})

async def s_typing(request):
    uid = auth(request); b = await request.json(); s = mine_session(uid, int(b["id"]))
    if s["status"] == "active": STYP[(s["id"], uid)] = time.time()
    return web.json_response({"ok": 1})

# ───────────────────────── Свой вход: логин + пароль ─────────────────────────
# Аккаунты сайта получают отрицательный uid, чтобы не пересекаться с Telegram ID (они всегда положительные).
LOGIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,24}$")
FAILS = {}                                # ключ -> [число попыток, начало окна]

def pwd_hash(p, salt=None):
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(p.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()

def pwd_check(p, stored):
    try: s, h = stored.split("$"); salt = bytes.fromhex(s)
    except ValueError: return False
    return hmac.compare_digest(pwd_hash(p, salt).split("$")[1], h)

def throttled(key, limit, win):
    e = FAILS.get(key)
    return bool(e and time.time() - e[1] <= win and e[0] >= limit)

def bump(key, win):
    now = time.time(); e = FAILS.get(key)
    if not e or now - e[1] > win: FAILS[key] = [1, now]
    else: e[0] += 1
    if len(FAILS) > 20000: FAILS.clear()

def new_web_session(uid):
    s = secrets.token_urlsafe(32)
    db.execute("INSERT INTO web_sessions VALUES(?,?,?)", (s, uid, time.time())); db.commit()
    return s

async def auth_register(request):
    ip = request.remote or ""
    if throttled("r:" + ip, 10, 3600): return web.json_response({"error": "slow"}, status=429)
    b = await request.json()
    lg, pw = str(b.get("login", "")).strip(), str(b.get("password", ""))
    if not LOGIN_RE.match(lg): return web.json_response({"error": "bad_login"})
    if not 8 <= len(pw) <= 128: return web.json_response({"error": "bad_pass"})
    key = lg.lower()
    if db.execute("SELECT 1 FROM accounts WHERE login=?", (key,)).fetchone(): return web.json_response({"error": "taken"})
    bump("r:" + ip, 3600)
    h = await asyncio.to_thread(pwd_hash, pw)
    uid = min(db.execute("SELECT MIN(uid) FROM users").fetchone()[0] or 0, 0) - 1
    try: db.execute("INSERT INTO accounts VALUES(?,?,?,?)", (key, uid, h, time.time()))
    except sqlite3.IntegrityError: return web.json_response({"error": "taken"})
    ensure_user(uid, lg)
    return web.json_response({"token": new_web_session(uid)})

DUMMY = pwd_hash("x")
async def auth_login(request):
    ip = request.remote or ""
    b = await request.json()
    key = str(b.get("login", "")).strip().lower(); pw = str(b.get("password", ""))[:128]
    if throttled("l:" + ip, 20, 600) or throttled("u:" + key, 8, 600): return web.json_response({"error": "slow"}, status=429)
    r = db.execute("SELECT uid,pwd FROM accounts WHERE login=?", (key,)).fetchone()
    ok = await asyncio.to_thread(pwd_check, pw, r["pwd"] if r else DUMMY)
    if not (r and ok):
        bump("l:" + ip, 600); bump("u:" + key, 600)
        return web.json_response({"error": "bad_creds"})
    return web.json_response({"token": new_web_session(r["uid"])})

# ───────────────────────── Вход на сайт через бота ─────────────────────────
LOGINS = {}                               # одноразовый токен -> [создан, uid или None]; живёт 10 минут

async def auth_start(request):
    now = time.time()
    for k in [k for k, v in LOGINS.items() if now - v[0] > 600]: del LOGINS[k]
    if len(LOGINS) > 5000: raise web.HTTPTooManyRequests()
    tok = secrets.token_urlsafe(24)
    LOGINS[tok] = [now, None]
    return web.json_response({"token": tok, "link": f"https://t.me/{ME['u']}?start=auth_{tok}"})

async def auth_poll(request):
    tok = request.query.get("t", "")
    e = LOGINS.get(tok)
    if not e or time.time() - e[0] > 600: return web.json_response({"error": "expired"})
    if e[1] is None: return web.json_response({"wait": 1})
    del LOGINS[tok]
    s = secrets.token_urlsafe(32)
    db.execute("INSERT INTO web_sessions VALUES(?,?,?)", (s, e[1], time.time())); db.commit()
    return web.json_response({"token": s})

async def auth_logout(request):
    tok = request.headers.get("X-Token", "")
    if tok: db.execute("DELETE FROM web_sessions WHERE token=?", (tok,)); db.commit()
    return web.json_response({"ok": 1})

def html_path():
    ps = [p for p in (os.path.join(BASE, "webapp", "index.html"), os.path.join(BASE, "index.html")) if os.path.exists(p)]
    return max(ps, key=os.path.getmtime) if ps else None   # берём самый свежий, если файлов два

async def index(request):
    p = html_path()
    if not p: raise web.HTTPNotFound(text="index.html not found")
    with open(p, encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html", headers={"Cache-Control": "no-cache"})

@web.middleware
async def gzip_mw(request, handler):
    resp = await handler(request)
    if isinstance(resp, web.Response) and isinstance(resp.body, (bytes, bytearray)) and len(resp.body) > 600:
        resp.enable_compression()   # JSON и HTML по ngrok уменьшаются в 3–8 раз
    return resp

# ───────────────────────── Бот ─────────────────────────
dp = Dispatcher()
BOT = Bot(TOKEN)
ME = {}

def kb(uid_or_lang="ru"):
    lang = uid_or_lang if uid_or_lang in BT else lang_of(uid_or_lang)
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=BT[lang]["open"], web_app=WebAppInfo(url=URL))]])

@dp.message(CommandStart())
async def start(m: Message, command: CommandObject):
    me = m.from_user
    db.execute("INSERT INTO users(uid,name) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name", (me.id, me.full_name)); db.commit()
    ensure_user(me.id, me.full_name)
    a = command.args or ""
    if a.startswith("auth_"):                                   # вход на сайт
        e = LOGINS.get(a[5:])
        if not e or time.time() - e[0] > 600:
            return await m.answer(bt(me.id, "login_bad"))
        mk = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=bt(me.id, "login_btn"), callback_data="lg:" + a[5:])]])
        return await m.answer(bt(me.id, "login_ask"), reply_markup=mk)
    if a.startswith("f") and a[1:].isdigit() and int(a[1:]) != me.id:
        fid = int(a[1:])
        if db.execute("SELECT 1 FROM users WHERE uid=?", (fid,)).fetchone():
            db.executemany("INSERT OR IGNORE INTO friends VALUES(?,?)", [(me.id, fid), (fid, me.id)]); db.commit()
            await m.answer(bt(me.id, "friends_ok"))
            try: await BOT.send_message(fid, bt(fid, "friend_added", name=me.full_name))
            except Exception: pass
    if a.startswith("w") and len(a) > 1:                       # ссылка-приглашение в сессию
        code, host = await join_by_token(me.id, a[1:])
        if code == "ok":
            return await m.answer(bt(me.id, "sess_joined", name=who(host, me.id)), reply_markup=kb(me.id))
        return await m.answer(bt(me.id, SESS_ERR.get(code, "sess_gone")))
    await m.answer(bt(me.id, "hello"), reply_markup=kb(me.id))

@dp.message(Command("lang"))
async def lang_cmd(m: Message, command: CommandObject):
    l = (command.args or "").strip().lower()[:2]
    if l not in ("ru", "en"):
        return await m.answer(bt(m.from_user.id, "lang_usage"))
    ensure_user(m.from_user.id, m.from_user.full_name)
    db.execute("UPDATE users SET lang=? WHERE uid=?", (l, m.from_user.id)); db.commit()
    LANG[m.from_user.id] = l
    await m.answer(BT[l]["lang_set"], reply_markup=kb(l))

@dp.message(Command("watch"))
async def watch_cmd(m: Message, command: CommandObject):
    """/watch anime <shikimori_id> | /watch movie <tmdb_id> -> кнопка, открывающая плеер внутри Telegram (WebApp)."""
    uid = m.from_user.id
    parts = (command.args or "").split()
    if len(parts) != 2 or parts[0].lower() not in ("anime", "movie") or not parts[1].isdigit():
        return await m.answer(bt(uid, "watch_usage"))
    kind, ident = parts[0].lower(), parts[1]
    try:
        player_url = await (get_anime_player(ident, HTTP) if kind == "anime" else get_movie_balancer(ident, HTTP))
    except Exception:
        log.exception("watch: ошибка получения плеера kind=%s id=%s", kind, ident)
        player_url = None
    log.info("watch uid=%s kind=%s id=%s -> %s", uid, kind, ident, player_url)
    if not player_url:
        return await m.answer(bt(uid, "watch_none"))
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=bt(uid, "watch_btn"), web_app=WebAppInfo(url=player_url))]])
    await m.answer(bt(uid, "watch_ready"), reply_markup=markup)

@dp.message(Command("remind"))
async def remind(m: Message, command: CommandObject):
    on = 0 if (command.args or "").strip() == "off" else 1
    db.execute("INSERT INTO users(uid,name,remind) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET remind=?", (m.from_user.id, m.from_user.full_name, on, on)); db.commit()
    await m.answer(bt(m.from_user.id, "remind_on" if on else "remind_off"))

@dp.message(Command("export"))
async def export(m: Message):
    rows = db.execute("SELECT title,year,type,status,rating,date,mood,note,genres,fav FROM items WHERE uid=? ORDER BY date", (m.from_user.id,)).fetchall()
    if not rows:
        return await m.answer(bt(m.from_user.id, "empty"))
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(rows[0].keys()); w.writerows([tuple(r) for r in rows])
    await m.answer_document(BufferedInputFile(("\ufeff" + buf.getvalue()).encode(), "my_cinema.csv"))

@dp.callback_query(F.data.startswith("lg:"))
async def cb_login(c: CallbackQuery):
    uid = c.from_user.id
    e = LOGINS.get(c.data[3:])
    if not e or time.time() - e[0] > 600:
        return await c.answer(bt(uid, "login_bad"), show_alert=True)
    e[1] = uid
    await c.answer()
    try: await c.message.edit_text(bt(uid, "login_ok"))
    except Exception: pass

@dp.callback_query(F.data.startswith("sa:"))
async def cb_sess_accept(c: CallbackQuery):
    uid = c.from_user.id
    try: sid = int(c.data[3:])
    except ValueError: return await c.answer()
    code = await accept_session(uid, sid)
    if code != "ok":
        await c.answer(bt(uid, SESS_ERR.get(code, "sess_state")), show_alert=True)
        try: await c.message.edit_reply_markup(reply_markup=kb(uid))
        except Exception: pass
        return
    await c.answer()
    try: await c.message.edit_text(bt(uid, "sess_ok"), reply_markup=kb(uid))
    except Exception: pass

@dp.callback_query(F.data.startswith("sd:"))
async def cb_sess_decline(c: CallbackQuery):
    uid = c.from_user.id
    try: sid = int(c.data[3:])
    except ValueError: return await c.answer()
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if s and s["guest"] == uid and s["status"] == "invited": bump(sid, status="ended")
    await c.answer()
    try: await c.message.edit_text(bt(uid, "sess_no"), reply_markup=kb(uid))
    except Exception: pass

async def reminders():
    while True:
        if datetime.now().hour == REMIND_HOUR:
            today = date.today()
            for u in db.execute("SELECT uid,last FROM users WHERE remind=1 AND uid>0").fetchall():
                if u["last"] and (today - date.fromisoformat(u["last"])).days < 3:
                    continue
                r = db.execute("SELECT title,title_en,year FROM items WHERE uid=? AND status='want' ORDER BY RANDOM() LIMIT 1", (u["uid"],)).fetchone()
                if r:
                    db.execute("UPDATE users SET last=? WHERE uid=?", (str(today), u["uid"])); db.commit()
                    title = (r["title_en"] or r["title"]) if lang_of(u["uid"]) == "en" else r["title"]
                    try: await BOT.send_message(u["uid"], bt(u["uid"], "reminder", title=title, year=r["year"]), reply_markup=kb(u["uid"]))
                    except Exception: pass
        await asyncio.sleep(1800)

async def ngrok_url():
    for _ in range(30):
        try:
            async with aiohttp.ClientSession() as ses:
                async with ses.get("http://127.0.0.1:4040/api/tunnels") as r:
                    for t in (await r.json()).get("tunnels", []):
                        if t["public_url"].startswith("https"):
                            return t["public_url"]
        except Exception:
            pass
        await asyncio.sleep(1)
    raise SystemExit("Не нашёл ngrok. Запусти его командой: ngrok http 8080")

def make_app():
    app = web.Application(client_max_size=4 * 1024 ** 2, middlewares=[gzip_mw])
    app.add_routes([web.get("/", index), web.post("/api/auth/register", auth_register), web.post("/api/auth/login", auth_login),
        web.post("/api/auth/start", auth_start), web.get("/api/auth/poll", auth_poll),
        web.post("/api/auth/logout", auth_logout), web.get("/api/search", search), web.get("/api/trending", trending),
        web.get("/api/items", items), web.post("/api/item", save), web.post("/api/item/delete", remove),
        web.get("/api/random", rnd), web.get("/api/discover", discover),
        web.get("/api/feed", feed), web.post("/api/dismiss", dismiss),
        web.get("/api/notes", notes), web.post("/api/notes", note_save), web.post("/api/notes/delete", note_del),
        web.get("/api/me", me), web.post("/api/me", me_set), web.get("/api/recs", recs),
        web.get("/api/settings", settings), web.post("/api/settings", settings), web.get("/api/invite", invite), web.get("/api/info", info), web.get("/api/players", players_api), web.get("/api/cinemas", cinemas_api), web.post("/api/embed", embed),
        web.get("/api/friends", friends), web.get("/api/friend/items", friend_items),
        web.post("/api/ping", ping), web.get("/api/lookup", lookup), web.post("/api/friends/add", friend_add),
        web.post("/api/friends/delete", friend_del), web.get("/api/chat", chat_get), web.post("/api/chat", chat_send),
        web.post("/api/session/invite", s_invite), web.get("/api/session/pending", s_pending), web.post("/api/session/respond", s_respond),
        web.get("/api/session", s_get), web.post("/api/session/media", s_media), web.post("/api/session/ready", s_ready),
        web.post("/api/session/ctl", s_ctl), web.post("/api/session/stop", s_stop),
        web.post("/api/session/link", s_link), web.get("/api/session/chat", s_chat_get), web.post("/api/session/chat", s_chat_send),
        web.get("/api/session/img", s_chat_img), web.post("/api/session/typing", s_typing)])
    return app

async def main():
    global URL, HTTP, WEB
    if URL.strip() in ("", "auto"):
        URL = await ngrok_url()
    print("Mini-app URL:", URL)
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                                 connector=aiohttp.TCPConnector(limit=30, ttl_dns_cache=300))   # одна сессия на всё
    WEB = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), connector=aiohttp.TCPConnector(resolver=SafeResolver(), limit=10))
    try:
        await BOT.set_chat_menu_button(menu_button=MenuButtonWebApp(text="MY CINEMA", web_app=WebAppInfo(url=URL)))
    except Exception as e:
        print("menu button:", e)
    ME["u"] = (await BOT.me()).username
    runner = web.AppRunner(make_app()); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    asyncio.create_task(reminders())
    try:
        await dp.start_polling(BOT)
    finally:
        await HTTP.close(); await WEB.close()

if __name__ == "__main__":
    asyncio.run(main())
