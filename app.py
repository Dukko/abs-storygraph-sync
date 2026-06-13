"""
ABS to StoryGraph Sync Service
Syncs audiobook listening progress from Audiobookshelf to StoryGraph
"""

from flask import Flask, request, jsonify
import os
import logging
import requests
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

ABS_URL = os.environ.get("ABS_URL", "").rstrip("/")  # e.g. http://audiobookshelf:13378
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

        # ABS stores authorName as a flat string for audiobooks
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
        self.driver = None

    def init_driver(self):
        opts = Options()
        opts.binary_location = "/usr/bin/chromium"
        opts.add_argument("--headless")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-setuid-sandbox")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--no-zygote")
        opts.add_argument("--single-process")
        opts.add_argument("--window-size=1280,900")
        service = Service("/usr/bin/chromedriver")
        self.driver = webdriver.Chrome(service=service, options=opts)
        logger.info("Chrome driver initialized")

    def _wait(self, timeout=10):
        return WebDriverWait(self.driver, timeout)

    def login(self) -> bool:
        try:
            self.driver.get("https://app.thestorygraph.com/users/sign_in")
            wait = self._wait()
            email_field = wait.until(EC.presence_of_element_located((By.ID, "user_email")))
            email_field.send_keys(self.email)
            self.driver.find_element(By.ID, "user_password").send_keys(self.password)
            self.driver.find_element(By.NAME, "commit").click()
            # Wait for redirect away from sign_in page
            wait.until(EC.url_changes("https://app.thestorygraph.com/users/sign_in"))
            logger.info("StoryGraph login successful")
            return True
        except Exception as e:
            logger.error("StoryGraph login failed: %s", e)
            return False

    def search_book(self, title: str, author: str) -> str | None:
        """Return the StoryGraph book URL for the best match, or None."""
        try:
            self.driver.get("https://app.thestorygraph.com/browse")
            wait = self._wait()
            search_box = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="search"]'))
            )
            search_box.clear()
            query = f"{title} {author}".strip()
            search_box.send_keys(query)
            search_box.submit()
            time.sleep(2)
            first_result = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a.book-title-link"))
            )
            url = first_result.get_attribute("href")
            logger.info("Found StoryGraph book: %s", url)
            return url
        except Exception as e:
            logger.error("Book search failed for '%s': %s", title, e)
            return None

    def update_progress(self, book_url: str, current_minutes: float) -> bool:
        """Navigate to the book page and set progress to current_minutes."""
        try:
            self.driver.get(book_url)
            wait = self._wait()

            # Open the update/log progress modal
            try:
                btn = wait.until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[contains(., 'Update') or contains(., 'Log')]")
                    )
                )
                btn.click()
            except TimeoutException:
                # Book isn't tracked yet — add it as currently reading first
                try:
                    self.driver.find_element(
                        By.XPATH, "//button[contains(., 'Want to Read')]"
                    ).click()
                    time.sleep(1)
                    self.driver.find_element(
                        By.XPATH, "//button[contains(., 'Currently Reading')]"
                    ).click()
                    time.sleep(1)
                except NoSuchElementException:
                    pass

                btn = wait.until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[contains(., 'Update') or contains(., 'Log')]")
                    )
                )
                btn.click()

            time.sleep(1)

            # Try to find a minutes-specific input first, then fall back to any number input
            minutes_value = str(int(current_minutes))
            try:
                inp = wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[id*='minute'], input[name*='minute'], input[placeholder*='minute']")
                    )
                )
            except TimeoutException:
                inp = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="number"]'))
                )

            inp.clear()
            inp.send_keys(minutes_value)

            self.driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
            time.sleep(2)
            logger.info("Updated '%s' to %s minutes", book_url, minutes_value)
            return True

        except Exception as e:
            logger.error("Failed to update progress for %s: %s", book_url, e)
            return False

    def close(self):
        if self.driver:
            self.driver.quit()
            self.driver = None


def _check_config():
    missing = [v for v in ("ABS_URL", "ABS_TOKEN", "STORYGRAPH_EMAIL", "STORYGRAPH_PASSWORD")
               if not os.environ.get(v)]
    return missing


@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "service": "ABS-StoryGraph Sync",
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

        syncer = StoryGraphSyncer(STORYGRAPH_EMAIL, STORYGRAPH_PASSWORD)
        syncer.init_driver()

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
