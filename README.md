# abs-storygraph-sync

Automatically syncs your [Audiobookshelf](https://www.audiobookshelf.org/) listening progress to [StoryGraph](https://www.thestorygraph.com/).

Runs as a lightweight Docker container alongside ABS. No browser automation — uses the ABS REST API and StoryGraph session cookies directly.

## Features

- Auto-syncs progress whenever you've listened to 5+ new minutes of a book
- Web UI to update credentials, view logs, and trigger a manual sync
- Progress is only pushed to StoryGraph when it actually changes (no duplicate journal entries)
- Settings and sync state persist across restarts

## Setup

### 1. Get your ABS API token

In Audiobookshelf: **Settings → Users → your user → API Token**

### 2. Get your StoryGraph session cookies

1. Log in to [app.thestorygraph.com](https://app.thestorygraph.com) in your browser
2. Open DevTools → **Application** → **Cookies** → `app.thestorygraph.com`
3. Copy the values for:
   - `_storygraph_session`
   - `remember_user_token`

### 3. Run with Docker Compose

```yaml
services:
  abs-storygraph-sync:
    image: ghcr.io/dukko/abs-storygraph-sync:latest
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./data:/app/data
    environment:
      PORT: "5465"
      ABS_URL: http://localhost:13378
      ABS_TOKEN: your_abs_api_token_here
      STORYGRAPH_SESSION: your_storygraph_session_cookie_here
      STORYGRAPH_REMEMBER_TOKEN: your_remember_user_token_here
```

```sh
docker compose up -d
```

Then open **http://your-server:5465** to access the dashboard.

> **Note:** The web UI has no authentication. Keep the port on your local network only — don't expose it to the internet.

## Configuration

All environment variables:

| Variable | Default | Description |
|---|---|---|
| `ABS_URL` | — | Base URL of your Audiobookshelf instance |
| `ABS_TOKEN` | — | ABS API token |
| `STORYGRAPH_SESSION` | — | `_storygraph_session` cookie value |
| `STORYGRAPH_REMEMBER_TOKEN` | — | `remember_user_token` cookie value |
| `PORT` | `5465` | Port for the web UI |
| `POLL_INTERVAL` | `120` | How often to check for new progress (seconds) |
| `SYNC_THRESHOLD_MINUTES` | `5` | Minimum new minutes listened before triggering a sync |
| `UI_PASSWORD` | *(unset)* | If set, enables HTTP Basic Auth on the web UI (any username) |

Credentials can also be updated at runtime via the web UI without restarting the container.

## How it works

1. Every `POLL_INTERVAL` seconds, fetches your in-progress books from the ABS API
2. If any book has gained `SYNC_THRESHOLD_MINUTES` or more minutes since the last check, it triggers a sync
3. For each book to sync, searches StoryGraph by title/author, sets it to "currently reading", and updates the progress percentage
4. Progress is only pushed if it changed by ≥ 0.5% since the last successful sync, preventing duplicate reading journal entries

StoryGraph has no public API — this tool uses session cookies to make the same requests the website does.

## Session cookie expiry

StoryGraph session cookies expire periodically. When the sync stops working, grab fresh cookies from your browser (step 2 above) and paste them into the **Settings** tab of the web UI.

## Credits

Inspired by [KOreader-storygraph](https://github.com/AsmaraLehrmann/KOreader-storygraph) and [storygraph.koplugin](https://github.com/burneracc0112/storygraph.koplugin).
