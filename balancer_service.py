"""
balancer_service.py — балансеры плееров для MY CINEMA.

Публичный API (имена и параметры совпадают с вызовами в bot.py):
    get_anime_player(shikimori_id, session=None)  -> str | None   (Kodik, аниме)
    get_movie_balancer(tmdb_id, session=None)     -> str | None   (Kodik по imdb -> иначе iframe Collaps)

Совместимость со старым bot.py (оставлены прежние имена):
    get_kodik_player(shikimori_id, session=None, imdb_id=None)
    get_collaps_player(tmdb_id, session=None, imdb_id=None, name=None)
    find_shikimori_id(names, year, session=None)

Все возвращаемые ссылки начинаются с "https://".
Если передать session=None, сессия создаётся и закрывается внутри функции.
"""
import asyncio
import logging
import os
import re
import socket
import time

import aiohttp

log = logging.getLogger("balancer")

# ───────────────────────── Настройки ─────────────────────────
DEFAULT_KODIK_TOKEN = "4c0708026894b9159740e531390fbc2a"
KODIK_MIRRORS = ("https://kodikapi.com", "https://kodik-api.com")
SHIKI_MIRRORS = ("https://shikimori.one", "https://shikimori.io")
# Collaps: прямой iframe без JSON API (JSON API требует платный токен).
# Домен v.collaps.org сейчас припаркован (показывает «created by ParkLogic»), поэтому каждый шаблон
# проверяется перед выдачей. Свои шаблоны можно задать в .env через "|":
#   COLLAPS_EMBED_TEMPLATES=https://host1/embed/imdb/{imdb_id}|https://host2/embed/movie/{tmdb_id}
# Доступные подстановки: {tmdb_id}, {imdb_id}.
COLLAPS_TEMPLATES = (
    "https://api.atomics.ws/embed/imdb/{imdb_id}",
    "https://api.bhcesh.me/embed/movie/{tmdb_id}",
    "https://v.collaps.org/embed/movie/{tmdb_id}",
)
DEAD_PAGE_MARKERS = ("parklogic", "this page has been created", "domain is for sale", "buy this domain",
                     "account suspended", "default web site page")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5, connect=3)
CACHE_TTL_OK = 3600        # найденная ссылка живёт час
CACHE_TTL_MISS = 300       # «не найдено» помним 5 минут, чтобы не долбить API
NET_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, socket.gaierror, OSError)

_cache: dict = {}          # key -> (expires_at, value)


# ───────────────────────── Утилиты ─────────────────────────
def normalize_url(url) -> str | None:
    """'//kodik.info/x' -> 'https://kodik.info/x'; 'http://' -> 'https://'."""
    if not url:
        return None
    url = str(url).strip()
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    if url.startswith("https://"):
        return url
    return "https://" + url.lstrip("/")


def _kodik_token() -> str:
    """Токен из KODIK_TOKEN, если он похож на настоящий (32 hex-символа), иначе публичный дефолтный."""
    tok = (os.getenv("KODIK_TOKEN") or "").strip().strip("'\"")
    if re.fullmatch(r"[0-9a-fA-F]{32}", tok):
        return tok
    if tok:
        log.warning("KODIK_TOKEN в .env выглядит некорректно — использую публичный токен по умолчанию")
    return DEFAULT_KODIK_TOKEN


def _cache_get(key):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return True, hit[1]
    return False, None


def _cache_put(key, value):
    _cache[key] = (time.time() + (CACHE_TTL_OK if value else CACHE_TTL_MISS), value)
    if len(_cache) > 2000:   # простая защита от разрастания
        now = time.time()
        for k in [k for k, v in _cache.items() if v[0] <= now]:
            _cache.pop(k, None)


class _Sess:
    """Контекст: использует переданную сессию или создаёт временную."""
    def __init__(self, session):
        self._given = session
        self._own = None

    async def __aenter__(self) -> aiohttp.ClientSession:
        if self._given is not None and not self._given.closed:
            return self._given
        self._own = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        return self._own

    async def __aexit__(self, *exc):
        if self._own is not None:
            await self._own.close()


async def _get_json(ses: aiohttp.ClientSession, url: str, params: dict, headers: dict | None = None):
    """GET -> (status, json|None). Бросает NET_ERRORS при сетевых сбоях."""
    async with ses.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT) as r:
        try:
            data = await r.json(content_type=None)
        except Exception:
            data = None
        return r.status, data


# ───────────────────────── Kodik ─────────────────────────
def _is_token_error(status: int, data) -> bool:
    err = str((data or {}).get("error") or "").lower() if isinstance(data, dict) else ""
    return status in (401, 403) or "токен" in err or "token" in err


def _pick_link(data) -> str | None:
    if not isinstance(data, dict):
        return None
    for item in data.get("results") or []:
        link = normalize_url(item.get("link"))
        if link:
            return link
    return None


async def _kodik_one(ses: aiohttp.ClientSession, mirror: str, token: str, query: dict):
    """Один запрос к одному зеркалу -> ("ok", link|None) | ("token", None) | ("fail", None)."""
    params = {"token": token, "with_episodes": "false", **query}
    try:
        status, data = await _get_json(ses, f"{mirror}/search", params)
    except NET_ERRORS as e:               # DNS-блокировка, таймаут, обрыв
        log.warning("Kodik %s недоступен (%s: %s)", mirror, type(e).__name__, e)
        return "fail", None
    if _is_token_error(status, data):
        log.warning("Kodik %s: токен отклонён (HTTP %s, %s)", mirror, status,
                    (data or {}).get("error") if isinstance(data, dict) else None)
        return "token", None
    if status != 200:
        log.warning("Kodik %s: HTTP %s", mirror, status)
        return "fail", None
    link = _pick_link(data)
    log.info("Kodik %s %s -> %s", mirror, query, link)
    return "ok", link


async def _kodik_search(ses: aiohttp.ClientSession, **query) -> str | None:
    """
    Поиск в Kodik: зеркала опрашиваются ОДНОВРЕМЕННО, побеждает первое ответившее.
    Токены: сначала из .env (если валиден), затем публичный по умолчанию.
    """
    tokens = [_kodik_token()]
    if DEFAULT_KODIK_TOKEN not in tokens:
        tokens.append(DEFAULT_KODIK_TOKEN)
    for token in tokens:
        tasks = [asyncio.ensure_future(_kodik_one(ses, m, token, query)) for m in KODIK_MIRRORS]
        try:
            for fut in asyncio.as_completed(tasks):
                kind, link = await fut
                if kind == "ok":
                    return link
        finally:
            for t in tasks:
                t.cancel()
    return None


async def get_anime_player(shikimori_id: str, session: aiohttp.ClientSession = None) -> str | None:
    """Ссылка на iframe-плеер Kodik по Shikimori ID или None."""
    sid = re.sub(r"\D", "", str(shikimori_id or ""))
    if not sid:
        return None
    key = ("kodik_shiki", sid)
    ok, val = _cache_get(key)
    if ok:
        return val
    async with _Sess(session) as ses:
        link = await _kodik_search(ses, shikimori_id=sid)
    _cache_put(key, link)
    return link


async def _kodik_by_imdb(imdb_id: str, session) -> str | None:
    imdb = str(imdb_id or "").strip()
    if not re.fullmatch(r"tt\d+", imdb):
        return None
    key = ("kodik_imdb", imdb)
    ok, val = _cache_get(key)
    if ok:
        return val
    async with _Sess(session) as ses:
        link = await _kodik_search(ses, imdb_id=imdb)
    _cache_put(key, link)
    return link


async def _imdb_from_tmdb(tmdb_id: str, session) -> str | None:
    """У Kodik нет поиска по TMDB ID, поэтому берём imdb_id у TMDB (нужен TMDB_KEY в .env)."""
    key_api = (os.getenv("TMDB_KEY") or "").strip()
    if not key_api:
        return None
    for kind in ("movie", "tv"):
        try:
            async with _Sess(session) as ses:
                status, data = await _get_json(
                    ses, f"https://api.themoviedb.org/3/{kind}/{tmdb_id}/external_ids", {"api_key": key_api})
        except NET_ERRORS as e:
            log.warning("TMDB external_ids недоступен (%s: %s)", type(e).__name__, e)
            return None
        if status == 200 and isinstance(data, dict) and data.get("imdb_id"):
            return data["imdb_id"]
    return None


# ───────────────────────── Collaps (без JSON API) ─────────────────────────
def _collaps_templates() -> list:
    raw = (os.getenv("COLLAPS_EMBED_TEMPLATES") or "").strip()
    return [t.strip() for t in raw.split("|") if t.strip()] if raw else list(COLLAPS_TEMPLATES)


def collaps_candidates(tmdb_id: str, imdb_id: str | None = None) -> list:
    """Список кандидатов-iframe без сетевых запросов (шаблоны, для которых не хватает ID, пропускаются)."""
    tid = re.sub(r"\D", "", str(tmdb_id or ""))
    imdb = str(imdb_id or "").strip()
    imdb = imdb if re.fullmatch(r"tt\d+", imdb) else ""
    out = []
    for t in _collaps_templates():
        if "{imdb_id}" in t and not imdb:
            continue
        if "{tmdb_id}" in t and not tid:
            continue
        out.append(normalize_url(t.format(tmdb_id=tid, imdb_id=imdb)))
    return out


async def _embed_alive(ses: aiohttp.ClientSession, url: str) -> bool:
    """Страница-плеер отвечает 200 и это не заглушка парковки домена. Это НЕ API Collaps, токен не нужен."""
    try:
        async with ses.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                           headers={"User-Agent": "Mozilla/5.0 (MyCinemaBot)"}) as r:
            if r.status >= 400:
                log.warning("embed %s -> HTTP %s", url, r.status)
                return False
            body = (await r.content.read(30000)).decode("utf-8", "ignore").lower()
    except NET_ERRORS as e:
        log.warning("embed %s недоступен (%s: %s)", url, type(e).__name__, e)
        return False
    if len(body.strip()) < 50 or any(m in body for m in DEAD_PAGE_MARKERS):
        log.warning("embed %s похож на заглушку/парковку домена", url)
        return False
    return True


async def collaps_player(tmdb_id: str, session=None, imdb_id: str | None = None) -> str | None:
    """Первый живой iframe-кандидат или None. Результат кэшируется."""
    cands = collaps_candidates(tmdb_id, imdb_id)
    if not cands:
        return None
    key = ("collaps", tuple(cands))
    ok, val = _cache_get(key)
    if ok:
        return val
    link = None
    async with _Sess(session) as ses:
        alive = await asyncio.gather(*(_embed_alive(ses, u) for u in cands))   # параллельно, порядок приоритета сохраняется
        link = next((u for u, ok_ in zip(cands, alive) if ok_), None)
    _cache_put(key, link)
    return link


# ───────────────────────── Фильмы / сериалы ─────────────────────────
async def get_movie_balancer(tmdb_id: str, session: aiohttp.ClientSession = None, *,
                             imdb_id: str | None = None) -> str | None:
    """
    1) Kodik (по imdb_id; если не передан — узнаём его у TMDB);
    2) если Kodik ничего не нашёл — iframe Collaps, но только если страница-плеер реально отвечает.
    None = плеера нет (бот покажет заглушку, а не мёртвый iframe).
    """
    tid = re.sub(r"\D", "", str(tmdb_id or ""))
    if not tid:
        return None

    try:
        imdb = imdb_id or await _imdb_from_tmdb(tid, session)
        link = await _kodik_by_imdb(imdb, session) if imdb else None
    except Exception as e:
        imdb = imdb_id                    # Kodik не должен ронять выдачу — есть запасной Collaps
        log.warning("Kodik для tmdb=%s упал: %s: %s", tid, type(e).__name__, e)
        link = None

    if link:
        log.info("get_movie_balancer tmdb=%s -> Kodik %s", tid, link)
        return link

    link = await collaps_player(tid, session, imdb)
    log.info("get_movie_balancer tmdb=%s -> Collaps %s", tid, link)
    return link


# ───────────────────────── Поиск Shikimori ID ─────────────────────────
def _norm(s) -> str:
    return re.sub(r"[\W_]+", " ", str(s or "").lower()).strip()


async def find_shikimori_id(names, year, session: aiohttp.ClientSession = None) -> str | None:
    """Ищет аниме на Shikimori по списку названий; при наличии года предпочитает совпадение по году."""
    names = [n for n in (names or []) if n]
    year = str(year or "")[:4]
    headers = {"User-Agent": "MyCinemaBot/1.0"}
    async with _Sess(session) as ses:
        for name in names:
            key = ("shiki", _norm(name), year)
            ok, val = _cache_get(key)
            if ok:
                if val:
                    return val
                continue
            found = None
            for mirror in SHIKI_MIRRORS:
                try:
                    status, data = await _get_json(
                        ses, f"{mirror}/api/animes", {"search": name, "limit": 10, "order": "popularity"}, headers)
                except NET_ERRORS as e:
                    log.warning("Shikimori %s недоступен (%s: %s)", mirror, type(e).__name__, e)
                    continue
                if status != 200 or not isinstance(data, list):
                    continue
                best = None
                for a in data:
                    aired = str(a.get("aired_on") or "")[:4]
                    if year and aired == year:
                        best = a
                        break
                    if best is None and not year:
                        best = a
                if best is None and data and not year:
                    best = data[0]
                found = str(best["id"]) if best else None
                break
            _cache_put(key, found)
            if found:
                return found
    return None


# ───────────────────────── Совместимость со старым bot.py ─────────────────────────
async def get_kodik_player(shikimori_id: str, session: aiohttp.ClientSession = None,
                           imdb_id: str | None = None) -> str | None:
    link = await get_anime_player(shikimori_id, session)
    if not link and imdb_id:
        link = await _kodik_by_imdb(imdb_id, session)
    return link


async def get_collaps_player(tmdb_id: str, session: aiohttp.ClientSession = None,
                             imdb_id: str | None = None, name: str | None = None) -> str | None:
    return await collaps_player(tmdb_id, session, imdb_id)
