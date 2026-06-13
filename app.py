"""
ABS to StoryGraph Sync Service
Syncs audiobook listening progress from Audiobookshelf to StoryGraph
"""

from flask import Flask, jsonify
import os
import logging
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

ABS_URL = os.environ.get("ABS_URL", "").rstrip("/")
ABS_TOKEN = os.environ.get("ABS_TOKEN")
STORYGRAPH_EMAIL = os.environ.get("STORYGRAPH_EMAIL")
STORYGRAPH_PASSWORD = os.environ.get("STORYGRAPH_PASSWORD")


def get_abs_in_progress():
    """Fetch in-progress audiobooks from the ABS API."""
    headers = {"Authorization": f"Bearer {ABS_TOKEN}"}
    resp = requests.get(f"{ABS_URL}/api/me/items-in-progress", headers=headers, timeout=10)
    resp.raise_for_status()

    books = []
    for item in resp.json().get("libraryItems", []):
        media = item.get("media", {})
        metadata = media.get("metadata", {})
        progress_data = item.get("userMediaProgress", {})

        title = metadata.get("title", "").strip()
        if not title:
            continue

        author = metadata.get("authorName", "")
        duration_sec = media.get("duration", 0) or 0
        current_sec = progress_data.get("currentTime", 0) or 0

        books.append({
            "title": title,
            "author": author,
            "duration_minutes": round(duration_sec / 60, 1),
            "current_minutes": round(current_sec / 60, 1),
            "progress_percent": round((progress_data.get("progress") or 0) * 100, 1),
        })

    logger.info("Found %d in-progress audiobook(s) in ABS", len(books))
    return books


class StoryGraphSyncer:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._playwright = None
        self._browser = None
        self.page = None

    def start(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        self.page = self._browser.new_page()
        logger.info("Playwright browser initialized")

    def login(self) -> bool:
        try:
            self.page.goto("https://app.thestorygraph.com/users/sign_in", wait_until="domcontentloaded")
            self.page.wait_for_selector("#user_email", timeout=15000)
            self.page.fill("#user_email", self.email)
            self.page.fill("#user_password", self.password)
            self.page.click("[name='commit']")
            self.page.wait_for_url(lambda url: "sign_in" not in url, timeout=15000)
            logger.info("StoryGraph login successful")
            return True
        except Exception as e:
            logger.error("StoryGraph login failed: %s", e)
            return False

    def search_book(self, title: str, author: str) -> str | None:
        try:
            self.page.goto("https://app.thestorygraph.com/browse", wait_until="domcontentloaded")
            self.page.fill('input[type="search"]', f"{title} {author}".strip())
            self.page.keyboard.press("Enter")
            self.page.wait_for_selector("a.book-title-link", timeout=10000)
            url = self.page.locator("a.book-title-link").first.get_attribute("href")
            logger.info("Found StoryGraph book: %s", url)
            return url
        except Exception as e:
            logger.error("Book search failed for '%s': %s", title, e)
            return None

    def update_progress(self, book_url: str, current_minutes: float) -> bool:
        try:
            self.page.goto(book_url, wait_until="domcontentloaded")
            minutes_value = str(int(current_minutes))

            # Open update/log progress modal; add to currently reading first if needed
            try:
                self.page.locator("button:has-text('Update'), button:has-text('Log')").first.click(timeout=8000)
            except PlaywrightTimeout:
                try:
                    self.page.locator("button:has-text('Want to Read')").first.click(timeout=5000)
                    self.page.locator("button:has-text('Currently Reading')").first.click(timeout=5000)
                except Exception:
                    pass
                self.page.locator("button:has-text('Update'), button:has-text('Log')").first.click(timeout=8000)

            self.page.wait_for_timeout(1000)

            # Try minutes input, fall back to any number input
            inp = (
                self.page.locator("input[id*='minute'], input[name*='minute'], input[placeholder*='minute']").first
                if self.page.locator("input[id*='minute'], input[name*='minute'], input[placeholder*='minute']").count() > 0
                else self.page.locator('input[type="number"]').first
            )
            inp.fill(minutes_value)
            self.page.locator('button[type="submit"]').click()
            self.page.wait_for_timeout(2000)
            logger.info("Updated '%s' to %s minutes", book_url, minutes_value)
            return True

        except Exception as e:
            logger.error("Failed to update progress for %s: %s", book_url, e)
            return False

    def close(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self._browser = None
        self._playwright = None


def _check_config():
    return [v for v in ("ABS_URL", "ABS_TOKEN", "STORYGRAPH_EMAIL", "STORYGRAPH_PASSWORD")
            if not os.environ.get(v)]


@app.route("/")
def home():
    return jsonify({"status": "running", "service": "ABS-StoryGraph Sync", "timestamp": datetime.now().isoformat()})


@app.route("/sync", methods=["POST"])
def sync():
    missing = _check_config()
    if missing:
        return jsonify({"error": f"Missing environment variables: {', '.join(missing)}"}), 500

    try:
        books = get_abs_in_progress()
        if not books:
            return jsonify({"message": "No in-progress audiobooks found in ABS", "synced": 0})

        syncer = StoryGraphSyncer(STORYGRAPH_EMAIL, STORYGRAPH_PASSWORD)
        syncer.start()

        if not syncer.login():
            syncer.close()
            return jsonify({"error": "Failed to login to StoryGraph"}), 500

        results = []
        for book in books:
            try:
                book_url = syncer.search_book(book["title"], book["author"])
                if book_url:
                    success = syncer.update_progress(book_url, book["current_minutes"])
                    results.append({
                        "title": book["title"],
                        "status": "success" if success else "failed",
                        "current_minutes": book["current_minutes"],
                        "duration_minutes": book["duration_minutes"],
                        "progress_percent": book["progress_percent"],
                    })
                else:
                    results.append({"title": book["title"], "status": "not_found"})
            except Exception as e:
                logger.error("Error syncing '%s': %s", book["title"], e)
                results.append({"title": book["title"], "status": "error", "error": str(e)})

        syncer.close()

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
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
