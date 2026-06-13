"""
ABS to StoryGraph Sync Service
"""

from flask import Flask, jsonify, request, render_template
import os, re, json, logging, threading, time
from collections import deque
from datetime import datetime
import requests as req
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

DATA_DIR = "/app/data"
CONFIG_FILE = f"{DATA_DIR}/config.json"
_runtime_config: dict = {}
_config_lock = threading.Lock()


def _load_file_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_file_config(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def cfg(key: str, default: str = "") -> str:
    with _config_lock:
        return _runtime_config.get(key) or os.environ.get(key, default)


# Load file config on startup (overrides env vars)
with _config_lock:
    _runtime_config.update(_load_file_config())

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 120))
SYNC_THRESHOLD = float(os.environ.get("SYNC_THRESHOLD_MINUTES", 5))
STORYGRAPH_BASE = "https://app.thestorygraph.com"

# ── Logging ───────────────────────────────────────────────────────────────────

class LogBuffer(logging.Handler):
    def __init__(self, maxlen=300):
        super().__init__()
        self._records: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record):
        with self._lock:
            self._records.append({
                "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "msg": record.getMessage(),
            })

    def get(self):
        with self._lock:
            return list(self._records)


_log_buffer = LogBuffer()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger().addHandler(_log_buffer)

# ── ABS ───────────────────────────────────────────────────────────────────────

def get_abs_in_progress() -> list[dict]:
    headers = {"Authorization": f"Bearer {cfg('ABS_TOKEN')}"}
    resp = req.get(f"{cfg('ABS_URL')}/api/me/items-in-progress", headers=headers, timeout=10)
    resp.raise_for_status()
    books = []
    for item in resp.json().get("libraryItems", []):
        media = item.get("media", {})
        metadata = media.get("metadata", {})
        title = metadata.get("title", "").strip()
        if not title:
            continue
        item_id = item.get("id", "")
        progress_data = {}
        if item_id:
            pr = req.get(f"{cfg('ABS_URL')}/api/me/progress/{item_id}", headers=headers, timeout=10)
            if pr.status_code == 200:
                progress_data = pr.json()
        books.append({
            "title": title,
            "author": metadata.get("authorName", ""),
            "progress_percent": round((progress_data.get("progress") or 0) * 100, 1),
            "current_minutes": round((progress_data.get("currentTime") or 0) / 60, 1),
            "duration_minutes": round((media.get("duration") or 0) / 60, 1),
        })
    logger.info("Found %d in-progress audiobook(s) in ABS", len(books))
    return books

# ── StoryGraph ────────────────────────────────────────────────────────────────

class StoryGraphClient:
    def __init__(self):
        self._session = req.Session()
        for name, val in [
            ("_storygraph_session", cfg("STORYGRAPH_SESSION")),
            ("remember_user_token", cfg("STORYGRAPH_REMEMBER_TOKEN")),
            ("cookies_popup_seen", "yes"),
            ("plus_popup_seen", "yes"),
        ]:
            self._session.cookies.set(name, val, domain="app.thestorygraph.com")
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept-Language": "en",
            "Origin": STORYGRAPH_BASE,
        })
        self._last_csrf = None

    def _extract_csrf(self, html):
        m = (
            re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html)
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', html)
            or re.search(r'<input[^>]+name=["\']authenticity_token["\'][^>]+value=["\']([^"\']+)["\']', html)
        )
        if m:
            self._last_csrf = m.group(1)
        return self._last_csrf

    def _get(self, path):
        resp = self._session.get(f"{STORYGRAPH_BASE}{path}", timeout=15)
        self._extract_csrf(resp.text)
        if "_storygraph_session" in resp.cookies:
            self._session.cookies.set("_storygraph_session", resp.cookies["_storygraph_session"], domain="app.thestorygraph.com")
        return resp

    def _post(self, path, data):
        return self._session.post(
            f"{STORYGRAPH_BASE}{path}", data=data,
            headers={
                "X-CSRF-Token": self._last_csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/javascript, application/javascript, */*; q=0.01",
                "Referer": STORYGRAPH_BASE,
            },
            allow_redirects=False, timeout=15,
        )

    def check_auth(self) -> bool:
        resp = self._get("/")
        return "sign_in" not in resp.url

    def search_book(self, title, author) -> str | None:
        query = req.utils.quote(f"{title} {author}".strip())
        resp = self._get(f"/browse?search_term={query}")
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        link = soup.find("a", class_="book-title-link")
        if not link:
            container = soup.find(class_="book-title-author-and-series")
            if container:
                link = container.find("a", href=re.compile(r"^/books/"))
        if link:
            m = re.search(r"/books/([^/?]+)", link.get("href", ""))
            if m:
                logger.info("Found '%s' -> id=%s", title, m.group(1))
                return m.group(1)
        logger.warning("No StoryGraph result for '%s'", title)
        return None

    def ensure_currently_reading(self, book_id):
        resp = self._get(f"/books/{book_id}")
        m = re.search(r'class="read-status-label"[^>]*>([^<]+)<', resp.text)
        status = m.group(1).strip().lower() if m else ""
        if "currently reading" in status or "rereading" in status:
            return True
        r = self._post(
            f"/update-status.js?book_id={book_id}&status=currently-reading",
            {"authenticity_token": self._last_csrf},
        )
        logger.info("Set currently-reading for %s: HTTP %s", book_id, r.status_code)
        return r.status_code in (200, 302)

    def update_progress(self, book_id, progress_percent) -> bool:
        resp = self._get(f"/books/{book_id}")
        m = re.search(
            r'(?:name="read_status\[book_num_of_pages\]"|class="read-status-book-num-of-pages")[^>]*value="([^"]*)"',
            resp.text,
        )
        book_pages = m.group(1) if m else "0"
        r = self._post("/update-progress", {
            "read_status[progress_number]": str(round(progress_percent, 1)),
            "read_status[progress_type]": "percentage",
            "read_status[book_num_of_pages]": book_pages,
            "book_id": book_id,
            "on_book_page": "true",
            "authenticity_token": self._last_csrf,
        })
        ok = r.status_code in (200, 302)
        logger.info("Progress for %s -> %.1f%%: HTTP %s", book_id, progress_percent, r.status_code)
        return ok

# ── Sync logic ────────────────────────────────────────────────────────────────

_last_synced: dict[str, float] = {}
_last_synced_lock = threading.Lock()

_status_cache: dict = {"books": [], "abs_ok": False, "ts": 0.0}
_status_cache_lock = threading.Lock()
STATUS_CACHE_TTL = 60  # seconds


def get_cached_books() -> tuple[list[dict], bool]:
    with _status_cache_lock:
        if time.time() - _status_cache["ts"] < STATUS_CACHE_TTL:
            return _status_cache["books"], _status_cache["abs_ok"]
    try:
        books = get_abs_in_progress()
        with _status_cache_lock:
            _status_cache.update({"books": books, "abs_ok": True, "ts": time.time()})
        return books, True
    except Exception:
        return _status_cache["books"], False


def do_sync(books: list[dict]) -> list[dict]:
    client = StoryGraphClient()
    if not client.check_auth():
        logger.error("StoryGraph session invalid — update STORYGRAPH_SESSION")
        return [{"title": b["title"], "status": "auth_error"} for b in books]
    results = []
    for book in books:
        try:
            book_id = client.search_book(book["title"], book["author"])
            if not book_id:
                results.append({"title": book["title"], "status": "not_found"})
                continue
            client.ensure_currently_reading(book_id)
            ok = client.update_progress(book_id, book["progress_percent"])
            results.append({
                "title": book["title"],
                "status": "success" if ok else "failed",
                "progress_percent": book["progress_percent"],
                "current_minutes": book["current_minutes"],
            })
        except Exception as e:
            logger.error("Error syncing '%s': %s", book["title"], e)
            results.append({"title": book["title"], "status": "error", "error": str(e)})
    return results


def _poll_loop():
    logger.info("Auto-sync started: polling every %ds, threshold %.1f min", POLL_INTERVAL, SYNC_THRESHOLD)
    while True:
        time.sleep(POLL_INTERVAL)
        if not all([cfg("ABS_URL"), cfg("ABS_TOKEN"), cfg("STORYGRAPH_SESSION")]):
            continue
        try:
            books = get_abs_in_progress()
            # update cache so UI shows fresh data after each poll
            with _status_cache_lock:
                _status_cache.update({"books": books, "abs_ok": True, "ts": time.time()})
            to_sync = []
            with _last_synced_lock:
                for b in books:
                    prev = _last_synced.get(b["title"], 0.0)
                    if b["current_minutes"] - prev >= SYNC_THRESHOLD:
                        logger.info("'%s' gained %.1f min — syncing", b["title"], b["current_minutes"] - prev)
                        to_sync.append(b)
            if to_sync:
                results = do_sync(to_sync)
                with _last_synced_lock:
                    for b in to_sync:
                        _last_synced[b["title"]] = b["current_minutes"]
                synced = sum(1 for r in results if r["status"] == "success")
                logger.info("Auto-sync: %d/%d synced", synced, len(to_sync))
        except Exception as e:
            logger.error("Auto-sync error: %s", e)

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    books, abs_ok = [], False
    if cfg("ABS_URL") and cfg("ABS_TOKEN"):
        books, abs_ok = get_cached_books()
    with _last_synced_lock:
        last = dict(_last_synced)
    return jsonify({
        "abs_ok": abs_ok,
        "sg_ok": bool(cfg("STORYGRAPH_SESSION")),
        "auto_sync": True,
        "poll_interval": POLL_INTERVAL,
        "sync_threshold": SYNC_THRESHOLD,
        "books": books,
        "last_synced": last,
    })


@app.route("/api/sync", methods=["POST"])
def api_sync():
    missing = [k for k in ("ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION") if not cfg(k)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 500
    try:
        books = get_abs_in_progress()
        if not books:
            return jsonify({"message": "No books in progress", "synced": 0, "total": 0, "results": []})
        results = do_sync(books)
        with _last_synced_lock:
            for b in books:
                _last_synced[b["title"]] = b["current_minutes"]
        synced = sum(1 for r in results if r["status"] == "success")
        return jsonify({"message": "Sync complete", "synced": synced, "total": len(books), "results": results})
    except Exception as e:
        logger.error("Sync failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": _log_buffer.get()})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.json or {}
    allowed = {"ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION", "STORYGRAPH_REMEMBER_TOKEN"}
    with _config_lock:
        for k, v in data.items():
            if k in allowed and v:
                _runtime_config[k] = v
        _save_file_config({k: v for k, v in _runtime_config.items() if k in allowed and v})
    logger.info("Settings updated via UI")
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Return current config keys (masked values) so the UI can show what's set."""
    return jsonify({
        "ABS_URL": cfg("ABS_URL"),
        "ABS_TOKEN": "set" if cfg("ABS_TOKEN") else "",
        "STORYGRAPH_SESSION": "set" if cfg("STORYGRAPH_SESSION") else "",
        "STORYGRAPH_REMEMBER_TOKEN": "set" if cfg("STORYGRAPH_REMEMBER_TOKEN") else "",
    })


@app.route("/debug-abs")
def debug_abs():
    headers = {"Authorization": f"Bearer {cfg('ABS_TOKEN')}"}
    resp = req.get(f"{cfg('ABS_URL')}/api/me/items-in-progress", headers=headers, timeout=10)
    return jsonify(resp.json())


if __name__ == "__main__":
    if all([cfg("ABS_URL"), cfg("ABS_TOKEN"), cfg("STORYGRAPH_SESSION")]):
        threading.Thread(target=_poll_loop, daemon=True).start()
    else:
        logger.warning("Missing config — auto-sync disabled")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
