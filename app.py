"""
ABS to StoryGraph Sync Service
Syncs audiobook listening progress from Audiobookshelf to StoryGraph
using direct HTTP requests with session cookies (no browser required).

Auto-syncs in the background whenever any book gains 5+ minutes of
listening time since the last sync.
"""

from flask import Flask, jsonify
import os
import re
import logging
import threading
import time
import requests as req
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

ABS_URL = os.environ.get("ABS_URL", "").rstrip("/")
ABS_TOKEN = os.environ.get("ABS_TOKEN")
STORYGRAPH_SESSION = os.environ.get("STORYGRAPH_SESSION")
STORYGRAPH_REMEMBER_TOKEN = os.environ.get("STORYGRAPH_REMEMBER_TOKEN", "")

# How often to poll ABS (seconds)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 120))
# Minimum new listening time (minutes) before triggering a sync
SYNC_THRESHOLD_MINUTES = float(os.environ.get("SYNC_THRESHOLD_MINUTES", 5))

STORYGRAPH_BASE = "https://app.thestorygraph.com"

# In-memory store: title → minutes at last sync
_last_synced: dict[str, float] = {}
_last_synced_lock = threading.Lock()


def get_abs_in_progress():
    """Fetch in-progress audiobooks from the ABS API."""
    headers = {"Authorization": f"Bearer {ABS_TOKEN}"}
    resp = req.get(f"{ABS_URL}/api/me/items-in-progress", headers=headers, timeout=10)
    resp.raise_for_status()

    books = []
    for item in resp.json().get("libraryItems", []):
        media = item.get("media", {})
        metadata = media.get("metadata", {})

        title = metadata.get("title", "").strip()
        if not title:
            continue

        # Progress is not embedded — fetch it separately
        item_id = item.get("id", "")
        progress_data = {}
        if item_id:
            prog_resp = req.get(f"{ABS_URL}/api/me/progress/{item_id}", headers=headers, timeout=10)
            if prog_resp.status_code == 200:
                progress_data = prog_resp.json()

        books.append({
            "title": title,
            "author": metadata.get("authorName", ""),
            "progress_percent": round((progress_data.get("progress") or 0) * 100, 1),
            "current_minutes": round((progress_data.get("currentTime") or 0) / 60, 1),
            "duration_minutes": round((media.get("duration") or 0) / 60, 1),
        })

    logger.info("Found %d in-progress audiobook(s) in ABS", len(books))
    return books


class StoryGraphClient:
    def __init__(self, session_cookie, remember_token):
        self._session = req.Session()
        self._session.cookies.set("_storygraph_session", session_cookie, domain="app.thestorygraph.com")
        self._session.cookies.set("remember_user_token", remember_token, domain="app.thestorygraph.com")
        self._session.cookies.set("cookies_popup_seen", "yes", domain="app.thestorygraph.com")
        self._session.cookies.set("plus_popup_seen", "yes", domain="app.thestorygraph.com")
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept-Language": "en",
            "Origin": STORYGRAPH_BASE,
            "Sec-Ch-Ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "DNT": "1",
        })
        self._last_csrf = None

    def _extract_csrf(self, html):
        match = (
            re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html)
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', html)
            or re.search(r'<input[^>]+name=["\']authenticity_token["\'][^>]+value=["\']([^"\']+)["\']', html)
        )
        if match:
            self._last_csrf = match.group(1)
        return self._last_csrf

    def _get(self, path):
        resp = self._session.get(f"{STORYGRAPH_BASE}{path}", timeout=15)
        self._extract_csrf(resp.text)
        if "_storygraph_session" in resp.cookies:
            self._session.cookies.set("_storygraph_session", resp.cookies["_storygraph_session"], domain="app.thestorygraph.com")
        return resp

    def _post(self, path, data):
        return self._session.post(
            f"{STORYGRAPH_BASE}{path}",
            data=data,
            headers={
                "X-CSRF-Token": self._last_csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/javascript, application/javascript, */*; q=0.01",
                "Referer": STORYGRAPH_BASE,
            },
            allow_redirects=False,
            timeout=15,
        )

    def check_auth(self):
        resp = self._get("/")
        return "sign_in" not in resp.url

    def search_book(self, title, author):
        query = req.utils.quote(f"{title} {author}".strip())
        resp = self._get(f"/browse?search_term={query}")
        if resp.status_code != 200:
            logger.warning("Search failed for '%s' (HTTP %s)", title, resp.status_code)
            return None
        match = re.search(r'href=["\'](/books/([^/"\'?]+))["\']', resp.text)
        if match:
            book_id = match.group(2)
            logger.info("Found book '%s' → id=%s", title, book_id)
            return book_id
        logger.warning("No StoryGraph result for '%s'", title)
        return None

    def ensure_currently_reading(self, book_id):
        resp = self._get(f"/books/{book_id}")
        status_match = re.search(r'class="read-status-label"[^>]*>([^<]+)<', resp.text)
        status = status_match.group(1).strip().lower() if status_match else ""
        if "currently reading" in status or "rereading" in status:
            return True
        update_resp = self._post(
            f"/update-status.js?book_id={book_id}&status=currently-reading",
            {"authenticity_token": self._last_csrf},
        )
        logger.info("Set currently-reading for %s: HTTP %s", book_id, update_resp.status_code)
        return update_resp.status_code in (200, 302)

    def update_progress(self, book_id, progress_percent):
        resp = self._get(f"/books/{book_id}")
        pages_match = re.search(
            r'(?:name="read_status\[book_num_of_pages\]"|class="read-status-book-num-of-pages")[^>]*value="([^"]*)"',
            resp.text,
        )
        book_pages = pages_match.group(1) if pages_match else "0"
        update_resp = self._post("/update-progress", {
            "read_status[progress_number]": str(round(progress_percent, 1)),
            "read_status[progress_type]": "percentage",
            "read_status[book_num_of_pages]": book_pages,
            "book_id": book_id,
            "on_book_page": "true",
            "authenticity_token": self._last_csrf,
        })
        ok = update_resp.status_code in (200, 302)
        logger.info("Updated progress for %s to %.1f%%: HTTP %s", book_id, progress_percent, update_resp.status_code)
        return ok


def do_sync(books_to_sync: list[dict]) -> list[dict]:
    """Sync a list of books to StoryGraph. Returns results list."""
    client = StoryGraphClient(STORYGRAPH_SESSION, STORYGRAPH_REMEMBER_TOKEN)
    if not client.check_auth():
        logger.error("StoryGraph session invalid — update STORYGRAPH_SESSION cookie")
        return []

    results = []
    for book in books_to_sync:
        try:
            book_id = client.search_book(book["title"], book["author"])
            if not book_id:
                results.append({"title": book["title"], "status": "not_found"})
                continue
            client.ensure_currently_reading(book_id)
            success = client.update_progress(book_id, book["progress_percent"])
            results.append({
                "title": book["title"],
                "status": "success" if success else "failed",
                "progress_percent": book["progress_percent"],
                "current_minutes": book["current_minutes"],
            })
        except Exception as e:
            logger.error("Error syncing '%s': %s", book["title"], e)
            results.append({"title": book["title"], "status": "error", "error": str(e)})
    return results


def _poll_loop():
    """Background thread: poll ABS and sync books that gained 5+ minutes."""
    logger.info("Auto-sync started: polling every %ds, threshold %.1f min", POLL_INTERVAL, SYNC_THRESHOLD_MINUTES)
    while True:
        time.sleep(POLL_INTERVAL)
        if not all([ABS_URL, ABS_TOKEN, STORYGRAPH_SESSION]):
            continue
        try:
            books = get_abs_in_progress()
            to_sync = []
            with _last_synced_lock:
                for book in books:
                    title = book["title"]
                    prev = _last_synced.get(title, 0.0)
                    gained = book["current_minutes"] - prev
                    if gained >= SYNC_THRESHOLD_MINUTES:
                        logger.info("'%s' gained %.1f min — queuing sync", title, gained)
                        to_sync.append(book)

            if to_sync:
                results = do_sync(to_sync)
                with _last_synced_lock:
                    for book in to_sync:
                        _last_synced[book["title"]] = book["current_minutes"]
                synced = len([r for r in results if r["status"] == "success"])
                logger.info("Auto-sync complete: %d/%d synced", synced, len(to_sync))

        except Exception as e:
            logger.error("Auto-sync poll error: %s", e)


def _check_config():
    return [v for v in ("ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION") if not os.environ.get(v)]


@app.route("/debug-abs")
def debug_abs():
    """Return raw ABS API response for inspection."""
    if not all([ABS_URL, ABS_TOKEN]):
        return jsonify({"error": "ABS_URL or ABS_TOKEN not set"}), 500
    headers = {"Authorization": f"Bearer {ABS_TOKEN}"}
    resp = req.get(f"{ABS_URL}/api/me/items-in-progress", headers=headers, timeout=10)
    return jsonify(resp.json())


@app.route("/")
def home():
    with _last_synced_lock:
        last = dict(_last_synced)
    return jsonify({
        "status": "running",
        "service": "ABS-StoryGraph Sync",
        "poll_interval_seconds": POLL_INTERVAL,
        "sync_threshold_minutes": SYNC_THRESHOLD_MINUTES,
        "last_synced": last,
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/sync", methods=["POST"])
def sync():
    missing = _check_config()
    if missing:
        return jsonify({"error": f"Missing environment variables: {', '.join(missing)}"}), 500

    try:
        books = get_abs_in_progress()
        if not books:
            return jsonify({"message": "No in-progress audiobooks found in ABS", "synced": 0})

        results = do_sync(books)

        with _last_synced_lock:
            for book in books:
                _last_synced[book["title"]] = book["current_minutes"]

        synced = len([r for r in results if r["status"] == "success"])
        return jsonify({
            "message": "Sync complete",
            "synced": synced,
            "total": len(books),
            "results": results,
            "timestamp": datetime.now().isoformat(),
        })

    except Exception as e:
        logger.error("Sync failed: %s", e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    if all([ABS_URL, ABS_TOKEN, STORYGRAPH_SESSION]):
        t = threading.Thread(target=_poll_loop, daemon=True)
        t.start()
    else:
        logger.warning("Missing config — auto-sync disabled until env vars are set")

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
