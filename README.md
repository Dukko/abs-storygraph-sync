# abs-storygraph-sync

Automatically syncs your [Audiobookshelf](https://www.audiobookshelf.org/) listening progress to [StoryGraph](https://www.thestorygraph.com/).

Runs as a lightweight Docker container alongside ABS. No browser automation — uses the ABS REST API and StoryGraph session cookies directly. Supports multiple people sharing one instance, each with their own ABS/StoryGraph credentials.

## Features

- Multi-user: everyone gets their own account, ABS/StoryGraph credentials, and sync state
- Login via local username/password, or SSO through any OIDC provider (e.g. [PocketID](https://github.com/pocket-id/pocket-id))
- Auto-syncs progress whenever you've listened to 5+ new minutes of a book
- Configurable sync scope: just in-progress books, in-progress + finished, or your entire library
- Web UI to manage credentials, view logs, and trigger a manual sync
- Progress is only pushed to StoryGraph when it actually changes (no duplicate journal entries)
- Accounts, settings, and sync state persist across restarts

## Setup

### 1. Run with Docker Compose

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
      # Optional: enable "Sign in with SSO" for any OIDC provider (e.g. PocketID)
      # OIDC_ISSUER: https://id.example.com
      # OIDC_CLIENT_ID: your_client_id
      # OIDC_CLIENT_SECRET: your_client_secret
```

```sh
docker compose up -d
```

Open **http://your-server:5465** — the first visit prompts you to create an account, which becomes an admin. Admins can add more local accounts from the **Users** panel; anyone who signs in via SSO gets an account automatically on first login.

### 2. Get your ABS API token

In Audiobookshelf: **Settings → Users → your user → API Token**

### 3. Get your StoryGraph session cookies

1. Log in to [app.thestorygraph.com](https://app.thestorygraph.com) in your browser
2. Open DevTools → **Application** → **Cookies** → `app.thestorygraph.com`
3. Copy the values for:
   - `_storygraph_session`
   - `remember_user_token`

### 4. Enter your credentials

Paste your ABS URL/token and StoryGraph cookies into the **Settings** card in the web UI (each account has its own). Optionally set the **Sync Scope** there too — see below.

## Configuration

Instance-wide environment variables (set once by whoever deploys the container):

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5465` | Port for the web UI |
| `POLL_INTERVAL` | `600` | How often to check for new progress (seconds) |
| `SYNC_THRESHOLD_MINUTES` | `5` | Minimum new minutes listened before triggering a sync |
| `OIDC_ISSUER` | *(unset)* | Base URL of your OIDC provider (must expose `/.well-known/openid-configuration`) |
| `OIDC_CLIENT_ID` | *(unset)* | OIDC client ID |
| `OIDC_CLIENT_SECRET` | *(unset)* | OIDC client secret |

Everything else — ABS credentials, StoryGraph cookies, sync scope — is per-user, set through the web UI, no restart needed.

## Sync scope

Each user picks how much of their library to sync, in **Settings**:

- **In Progress** (default) — only books you're currently listening to
- **+ Finished** — the above, plus books you've completed (marked "read" on StoryGraph)
- **Entire Library** — every book, including ones you haven't started (marked "to-read" on StoryGraph)

## How it works

1. Every `POLL_INTERVAL` seconds, fetches each user's books from the ABS API (scoped per their Sync Scope setting)
2. If any book has gained `SYNC_THRESHOLD_MINUTES` or more minutes since the last check, or has just been finished, it triggers a sync
3. For each book to sync, searches StoryGraph by title/author, sets its status (to-read / currently-reading / read), and updates the progress percentage
4. Progress/status is only pushed if it actually changed since the last successful sync, preventing duplicate reading journal entries

StoryGraph has no public API — this tool uses session cookies to make the same requests the website does.

> **⚠️ Fragility warning:** Because this tool reverse-engineers StoryGraph's internal, unpublished web endpoints, it is inherently brittle. Any change StoryGraph makes to their HTML structure, URL routes, CSRF handling, or cookie behaviour can break the sync without warning and with no recourse. There is no official API to fall back on.

## Session cookie expiry

StoryGraph session cookies expire periodically. When the sync stops working, grab fresh cookies from your browser (step 2 above) and paste them into the **Settings** tab of the web UI.

## Credits

Inspired by [KOreader-storygraph](https://github.com/AsmaraLehrmann/KOreader-storygraph) and [storygraph.koplugin](https://github.com/burneracc0112/storygraph.koplugin).
