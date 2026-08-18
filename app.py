"""
ABS to StoryGraph Sync Service
"""

from flask import Flask, jsonify, request, render_template, session, redirect, url_for, g
from urllib.parse import urlparse
from functools import wraps
import os, re, json, logging, threading, time, uuid
from collections import deque
from datetime import datetime, timezone
import requests as req
from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth

# ── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR = "/app/data"
USERS_FILE = f"{DATA_DIR}/users.json"
CONFIG_DIR = f"{DATA_DIR}/config"
SYNC_STATE_DIR = f"{DATA_DIR}/sync_state"

STORYGRAPH_BASE = "https://app.thestorygraph.com"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 600))
SYNC_THRESHOLD = float(os.environ.get("SYNC_THRESHOLD_MINUTES", 5))

OIDC_ISSUER = os.environ.get("OIDC_ISSUER")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
OIDC_ENABLED = bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)

SYNC_SCOPES = ("in_progress", "in_progress_finished", "library")


def _get_secret_key() -> bytes:
    key_file = f"{DATA_DIR}/secret_key"
    try:
        with open(key_file, "rb") as f:
            key = f.read()
            if len(key) >= 32:
                return key
    except FileNotFoundError:
        pass
    key = os.urandom(32)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(key_file, "wb") as f:
        f.write(key)
    return key

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
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # suppress per-request access logs

# ── User store ───────────────────────────────────────────────────────────────

_users_lock = threading.Lock()


def _load_users() -> list[dict]:
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_users(users: list[dict]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def any_users_exist() -> bool:
    with _users_lock:
        return bool(_load_users())


def list_users() -> list[dict]:
    with _users_lock:
        return _load_users()


def get_user(user_id: str) -> dict | None:
    with _users_lock:
        return next((u for u in _load_users() if u["id"] == user_id), None)


def get_user_by_username(username: str) -> dict | None:
    username = (username or "").lower()
    with _users_lock:
        return next((u for u in _load_users() if (u.get("username") or "").lower() == username), None)


def get_user_by_oidc_sub(sub: str) -> dict | None:
    with _users_lock:
        return next((u for u in _load_users() if u.get("oidc_sub") == sub), None)


def create_user(*, username=None, password=None, oidc_sub=None, display_name=None, is_admin=False) -> dict:
    with _users_lock:
        users = _load_users()
        user = {
            "id": uuid.uuid4().hex,
            "username": username,
            "password_hash": generate_password_hash(password) if password else None,
            "oidc_sub": oidc_sub,
            "display_name": display_name or username or "User",
            "is_admin": is_admin,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        users.append(user)
        _save_users(users)
        return user


def update_user(user_id: str, **fields):
    with _users_lock:
        users = _load_users()
        for u in users:
            if u["id"] == user_id:
                u.update(fields)
        _save_users(users)


def delete_user(user_id: str):
    with _users_lock:
        users = [u for u in _load_users() if u["id"] != user_id]
        _save_users(users)

# ── Per-user config ──────────────────────────────────────────────────────────

_config_lock = threading.Lock()
_runtime_config: dict[str, dict] = {}


def _config_file(user_id: str) -> str:
    return f"{CONFIG_DIR}/{user_id}.json"


def _load_user_config(user_id: str) -> dict:
    try:
        with open(_config_file(user_id)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_user_config(user_id: str, data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(_config_file(user_id), "w") as f:
        json.dump(data, f, indent=2)


def cfg(user_id: str, key: str, default: str = "") -> str:
    with _config_lock:
        if user_id not in _runtime_config:
            _runtime_config[user_id] = _load_user_config(user_id)
        return _runtime_config[user_id].get(key) or default


def set_cfg(user_id: str, updates: dict):
    with _config_lock:
        if user_id not in _runtime_config:
            _runtime_config[user_id] = _load_user_config(user_id)
        _runtime_config[user_id].update({k: v for k, v in updates.items() if v})
        _save_user_config(user_id, _runtime_config[user_id])

# ── Per-user sync state ──────────────────────────────────────────────────────
# Tracks the last {progress %, StoryGraph status} successfully pushed per book, persisted across restarts.

_synced_state: dict[str, dict] = {}
_synced_state_lock = threading.Lock()


def _sync_state_file(user_id: str) -> str:
    return f"{SYNC_STATE_DIR}/{user_id}.json"


def _load_sync_state(user_id: str) -> dict:
    try:
        with open(_sync_state_file(user_id)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sync_state(user_id: str, data: dict):
    os.makedirs(SYNC_STATE_DIR, exist_ok=True)
    with open(_sync_state_file(user_id), "w") as f:
        json.dump(data, f, indent=2)


def _get_synced(user_id: str) -> dict:
    with _synced_state_lock:
        if user_id not in _synced_state:
            _synced_state[user_id] = _load_sync_state(user_id)
        return _synced_state[user_id]

# ── ABS ───────────────────────────────────────────────────────────────────────

def _abs_headers(user_id: str) -> dict:
    return {"Authorization": f"Bearer {cfg(user_id, 'ABS_TOKEN')}"}


def _abs_get(user_id: str, path: str, params: dict | None = None):
    resp = req.get(f"{cfg(user_id, 'ABS_URL')}{path}", headers=_abs_headers(user_id), params=params, timeout=10)
    resp.raise_for_status()
    return resp


def _item_to_book(item: dict, progress: dict) -> dict | None:
    media = item.get("media", {})
    metadata = media.get("metadata", {})
    title = metadata.get("title", "").strip()
    if not title:
        return None
    return {
        "title": title,
        "author": metadata.get("authorName", ""),
        "progress_percent": round((progress.get("progress") or 0) * 100, 1),
        "current_minutes": round((progress.get("currentTime") or 0) / 60, 1),
        "duration_minutes": round((media.get("duration") or 0) / 60, 1),
        "is_finished": bool(progress.get("isFinished")),
    }


def get_abs_books(user_id: str, scope: str) -> list[dict]:
    if scope == "in_progress":
        resp = _abs_get(user_id, "/api/me/items-in-progress")
        books = []
        for item in resp.json().get("libraryItems", []):
            item_id = item.get("id", "")
            progress = {}
            if item_id:
                pr = req.get(f"{cfg(user_id, 'ABS_URL')}/api/me/progress/{item_id}", headers=_abs_headers(user_id), timeout=10)
                if pr.status_code == 200:
                    progress = pr.json()
            book = _item_to_book(item, progress)
            if book:
                books.append(book)
        return books

    # in_progress_finished / library both need every progress entry the user has
    me = _abs_get(user_id, "/api/me").json()
    progress_by_item = {p["libraryItemId"]: p for p in me.get("mediaProgress", []) if p.get("libraryItemId")}

    if scope == "in_progress_finished":
        books = []
        for item_id, progress in progress_by_item.items():
            if not ((progress.get("currentTime") or 0) > 0 or progress.get("isFinished")):
                continue
            item_resp = req.get(f"{cfg(user_id, 'ABS_URL')}/api/items/{item_id}", headers=_abs_headers(user_id), timeout=10)
            if item_resp.status_code != 200:
                continue
            book = _item_to_book(item_resp.json(), progress)
            if book:
                books.append(book)
        return books

    # scope == "library": every book, including untouched ones
    libraries = _abs_get(user_id, "/api/libraries").json().get("libraries", [])
    books = []
    for lib in libraries:
        page = 0
        while True:
            resp = _abs_get(user_id, f"/api/libraries/{lib['id']}/items", params={"limit": 100, "page": page})
            data = resp.json()
            results = data.get("results", [])
            total = data.get("total", len(results))
            for item in results:
                progress = progress_by_item.get(item.get("id"), {})
                book = _item_to_book(item, progress)
                if book:
                    books.append(book)
            page += 1
            if not results or page * 100 >= total:
                break
    return books

# ── StoryGraph ────────────────────────────────────────────────────────────────

# StoryGraph has no public API — these status slugs are reverse-engineered from
# the site's own /update-status.js requests and may change without notice.
_STATUS_LABELS = {
    "to-read": "to read",
    "currently-reading": "currently reading",
    "read": "read",
}


class StoryGraphClient:
    def __init__(self, session_cookie: str, remember_token: str = ""):
        self._session = req.Session()
        for name, val in [
            ("_storygraph_session", session_cookie),
            ("remember_user_token", remember_token),
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

    def get_book_page(self, book_id) -> str:
        return self._get(f"/books/{book_id}").text

    def _parse_current_progress(self, html) -> float | None:
        """Best-effort read of the percentage StoryGraph currently has on file for
        this book, from the same hidden field update_progress() fills in. Returns
        None if it can't be found — callers should treat that as 'unknown' and not
        skip a write on its account."""
        m = re.search(
            r'(?:name="read_status\[progress_number\]"|class="read-status-progress-number")[^>]*value="([^"]*)"',
            html,
        )
        if not m or not m.group(1):
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None

    def ensure_status(self, book_id, target_status, html: str | None = None) -> tuple[bool, bool, str | None]:
        """Returns (ok, already_matched, html) — already_matched is True when the book's
        StoryGraph status already matched target_status and nothing was posted. html is
        the page markup the match was read from (reusable for a progress check), or None
        if a status-changing POST was made and any previously-fetched markup is now stale."""
        html = html if html is not None else self.get_book_page(book_id)
        m = re.search(r'class="read-status-label"[^>]*>([^<]+)<', html)
        current = m.group(1).strip().lower() if m else ""
        label = _STATUS_LABELS[target_status]
        if label in current or (target_status == "currently-reading" and "rereading" in current):
            return True, True, html
        r = self._post(
            f"/update-status.js?book_id={book_id}&status={target_status}",
            {"authenticity_token": self._last_csrf},
        )
        logger.info("Set status=%s for %s: HTTP %s", target_status, book_id, r.status_code)
        return r.status_code in (200, 302), False, None

    def update_progress(self, book_id, progress_percent, html: str | None = None) -> bool:
        html = html if html is not None else self.get_book_page(book_id)
        m = re.search(
            r'(?:name="read_status\[book_num_of_pages\]"|class="read-status-book-num-of-pages")[^>]*value="([^"]*)"',
            html,
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

_last_synced: dict[str, dict[str, float]] = {}
_last_synced_lock = threading.Lock()

_status_cache: dict[str, dict] = {}
_status_cache_lock = threading.Lock()
STATUS_CACHE_TTL = 60  # seconds


def get_cached_books(user_id: str, scope: str) -> tuple[list[dict], bool]:
    with _status_cache_lock:
        cache = _status_cache.get(user_id, {"books": [], "abs_ok": False, "ts": 0.0})
        if time.time() - cache["ts"] < STATUS_CACHE_TTL:
            return cache["books"], cache["abs_ok"]
    try:
        books = get_abs_books(user_id, scope)
        with _status_cache_lock:
            _status_cache[user_id] = {"books": books, "abs_ok": True, "ts": time.time()}
        return books, True
    except Exception:
        return cache["books"], False


def _target_status(book: dict) -> str:
    if book.get("is_finished"):
        return "read"
    if book["progress_percent"] > 0:
        return "currently-reading"
    return "to-read"


def do_sync(user_id: str, books: list[dict]) -> list[dict]:
    user = get_user(user_id)
    label = (user or {}).get("username") or (user or {}).get("display_name") or user_id
    client = StoryGraphClient(cfg(user_id, "STORYGRAPH_SESSION"), cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN"))
    if not client.check_auth():
        logger.error("[%s] StoryGraph session invalid — update STORYGRAPH_SESSION", label)
        return [{"title": b["title"], "status": "auth_error"} for b in books]
    synced = _get_synced(user_id)
    results = []
    for book in books:
        try:
            pct = book["progress_percent"]
            status = _target_status(book)
            prev = synced.get(book["title"])
            if prev is not None and prev.get("status") == status and abs(pct - prev.get("pct", -1)) < 0.5:
                logger.info("[%s] '%s' unchanged (%s, %.1f%%) — skipping", label, book["title"], status, pct)
                results.append({"title": book["title"], "status": "unchanged", "progress_percent": pct})
                continue

            book_id = client.search_book(book["title"], book["author"])
            if not book_id:
                results.append({"title": book["title"], "status": "not_found"})
                continue
            ok, already_matched, status_html = client.ensure_status(book_id, status)
            target_pct = 100 if status == "read" else pct
            # Skip the progress POST when StoryGraph already agrees with us. "read" is
            # always 100% by definition, so an already-matched read status is always
            # safe to skip (this was the original fix for finished-book duplicates).
            # For "currently reading" we can't assume that — the status label matching
            # doesn't tell us the percentage matches too — so we additionally parse the
            # progress StoryGraph has on file and only skip if it's already within
            # rounding distance of what we'd post. Without this, a book that already
            # matched (e.g. every "currently reading" book on a fresh install, before
            # this tool has any local sync-state for it) still got a progress POST on
            # every single poll, creating a spurious "started" entry and a duplicate
            # progress update in the StoryGraph journal even though nothing about the
            # book had actually changed. If we can't parse the current percentage at
            # all, we conservatively still post — no worse than the old behavior.
            current_pct = client._parse_current_progress(status_html or "") if already_matched else None
            skip_progress = status == "to-read" or (
                already_matched
                and (status == "read" or (current_pct is not None and abs(current_pct - target_pct) < 0.5))
            )
            if not skip_progress:
                ok = client.update_progress(book_id, target_pct, html=status_html)
            if ok:
                synced[book["title"]] = {"pct": pct, "status": status}
                _save_sync_state(user_id, synced)
            results.append({
                "title": book["title"],
                "status": "success" if ok else "failed",
                "progress_percent": pct,
                "current_minutes": book["current_minutes"],
            })
        except Exception as e:
            logger.error("[%s] Error syncing '%s': %s", label, book["title"], e)
            results.append({"title": book["title"], "status": "error", "error": str(e)})
    return results


def _poll_user(user: dict):
    user_id = user["id"]
    if not all([cfg(user_id, "ABS_URL"), cfg(user_id, "ABS_TOKEN"), cfg(user_id, "STORYGRAPH_SESSION")]):
        return
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    label = user.get("username") or user.get("display_name") or user_id
    try:
        books = get_abs_books(user_id, scope)
        with _status_cache_lock:
            _status_cache[user_id] = {"books": books, "abs_ok": True, "ts": time.time()}
        to_sync = []
        with _last_synced_lock:
            last = _last_synced.setdefault(user_id, {})
            for b in books:
                prev = last.get(b["title"], 0.0)
                if b["is_finished"] or b["current_minutes"] - prev >= SYNC_THRESHOLD:
                    to_sync.append(b)
        if to_sync:
            results = do_sync(user_id, to_sync)
            with _last_synced_lock:
                for b in to_sync:
                    _last_synced[user_id][b["title"]] = b["current_minutes"]
            synced = sum(1 for r in results if r["status"] == "success")
            logger.info("[%s] Auto-sync: %d/%d synced", label, synced, len(to_sync))
    except Exception as e:
        logger.error("[%s] Auto-sync error: %s", label, e)


def _poll_loop():
    logger.info("Auto-sync started: polling every %ds, threshold %.1f min", POLL_INTERVAL, SYNC_THRESHOLD)
    while True:
        time.sleep(POLL_INTERVAL)
        for user in list_users():
            _poll_user(user)

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = _get_secret_key()

oauth = OAuth(app)
if OIDC_ENABLED:
    oauth.register(
        name="oidc",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f"{OIDC_ISSUER.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )

PUBLIC_ENDPOINTS = {"login", "login_oidc", "auth_callback", "logout", "setup", "static"}


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user or not g.user.get("is_admin"):
            return jsonify({"error": "Forbidden"}), 403
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def load_user():
    g.user = None
    if request.endpoint == "static":
        return
    user_id = session.get("user_id")
    if user_id:
        g.user = get_user(user_id)
        if g.user is None:
            session.clear()
    if not any_users_exist():
        if request.endpoint != "setup":
            return redirect(url_for("setup"))
        return
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if g.user is None:
        return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if any_users_exist():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "Username and password are required."
        else:
            user = create_user(username=username, password=password, is_admin=True)
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        user = get_user_by_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        if user and user.get("password_hash") and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error, oidc_enabled=OIDC_ENABLED)


@app.route("/login/oidc")
def login_oidc():
    if not OIDC_ENABLED:
        return redirect(url_for("login"))
    return oauth.oidc.authorize_redirect(url_for("auth_callback", _external=True))


@app.route("/auth/callback")
def auth_callback():
    if not OIDC_ENABLED:
        return redirect(url_for("login"))
    token = oauth.oidc.authorize_access_token()
    claims = token.get("userinfo") or oauth.oidc.userinfo(token=token)
    sub = claims.get("sub")
    if not sub:
        return redirect(url_for("login"))
    user = get_user_by_oidc_sub(sub)
    if not user:
        display_name = claims.get("preferred_username") or claims.get("email") or sub
        user = create_user(oidc_sub=sub, display_name=display_name, is_admin=False)
    session["user_id"] = user["id"]
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template(
        "index.html",
        is_admin=bool(g.user.get("is_admin")),
        display_name=g.user.get("display_name") or g.user.get("username"),
    )


@app.route("/api/status")
def api_status():
    user_id = g.user["id"]
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    books, abs_ok = [], False
    if cfg(user_id, "ABS_URL") and cfg(user_id, "ABS_TOKEN"):
        books, abs_ok = get_cached_books(user_id, scope)
    with _last_synced_lock:
        last = dict(_last_synced.get(user_id, {}))
    return jsonify({
        "abs_ok": abs_ok,
        "sg_ok": bool(cfg(user_id, "STORYGRAPH_SESSION")),
        "auto_sync": True,
        "poll_interval": POLL_INTERVAL,
        "sync_threshold": SYNC_THRESHOLD,
        "sync_scope": scope,
        "books": books,
        "last_synced": last,
    })


@app.route("/api/sync", methods=["POST"])
def api_sync():
    user_id = g.user["id"]
    missing = [k for k in ("ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION") if not cfg(user_id, k)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 500
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    try:
        books = get_abs_books(user_id, scope)
        if not books:
            return jsonify({"message": "No books found", "synced": 0, "total": 0, "results": []})
        results = do_sync(user_id, books)
        with _last_synced_lock:
            last = _last_synced.setdefault(user_id, {})
            for b in books:
                last[b["title"]] = b["current_minutes"]
        synced = sum(1 for r in results if r["status"] == "success")
        return jsonify({"message": "Sync complete", "synced": synced, "total": len(books), "results": results})
    except Exception as e:
        logger.error("Sync failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/logs")
@admin_required
def api_logs():
    return jsonify({"logs": _log_buffer.get()})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    user_id = g.user["id"]
    data = request.json or {}
    allowed = {"ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION", "STORYGRAPH_REMEMBER_TOKEN", "SYNC_SCOPE"}
    if "ABS_URL" in data and data["ABS_URL"]:
        parsed = urlparse(data["ABS_URL"])
        if parsed.scheme not in ("http", "https"):
            return jsonify({"error": "ABS_URL must use http or https"}), 400
        if not parsed.hostname:
            return jsonify({"error": "ABS_URL must include a hostname"}), 400
    if data.get("SYNC_SCOPE") and data["SYNC_SCOPE"] not in SYNC_SCOPES:
        return jsonify({"error": "Invalid SYNC_SCOPE"}), 400
    set_cfg(user_id, {k: v for k, v in data.items() if k in allowed})
    logger.info("[%s] Settings updated via UI", g.user.get("username") or g.user.get("display_name") or user_id)
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Return current config keys (masked values) so the UI can show what's set."""
    user_id = g.user["id"]
    return jsonify({
        "ABS_URL": cfg(user_id, "ABS_URL"),
        "ABS_TOKEN": "set" if cfg(user_id, "ABS_TOKEN") else "",
        "STORYGRAPH_SESSION": "set" if cfg(user_id, "STORYGRAPH_SESSION") else "",
        "STORYGRAPH_REMEMBER_TOKEN": "set" if cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN") else "",
        "SYNC_SCOPE": cfg(user_id, "SYNC_SCOPE", "in_progress"),
    })


@app.route("/api/users")
@admin_required
def api_users():
    users = [{
        "id": u["id"],
        "username": u.get("username"),
        "display_name": u.get("display_name"),
        "is_admin": bool(u.get("is_admin")),
        "via_oidc": bool(u.get("oidc_sub")),
    } for u in list_users()]
    return jsonify({"users": users})


@app.route("/api/users", methods=["POST"])
@admin_required
def api_create_user():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if get_user_by_username(username):
        return jsonify({"error": "Username already taken"}), 400
    user = create_user(username=username, password=password, is_admin=bool(data.get("is_admin")))
    return jsonify({"id": user["id"]})


@app.route("/api/users/<user_id>", methods=["DELETE"])
@admin_required
def api_delete_user(user_id):
    if user_id == g.user["id"]:
        return jsonify({"error": "Cannot delete your own account"}), 400
    delete_user(user_id)
    return jsonify({"ok": True})


@app.route("/api/users/<user_id>/admin", methods=["POST"])
@admin_required
def api_set_admin(user_id):
    if user_id == g.user["id"]:
        return jsonify({"error": "Cannot change your own admin status"}), 400
    data = request.json or {}
    update_user(user_id, is_admin=bool(data.get("is_admin")))
    return jsonify({"ok": True})


if __name__ == "__main__":
    threading.Thread(target=_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
