# CSTA Copilot — Ticket Lens

A small Flask tool that uses your browser session cookie (no API key needed) to
pull Zendesk tickets and generate "30-second briefs" by shelling out to the
Claude Code CLI.

> **Note:** Resource Lens (Confluent Cloud environments/clusters) now lives in a
> separate app under `../resource-lens`. The in-app Settings page is disabled —
> cookies are managed through `.env` (see below).

## Prerequisites

- Python 3.9+
- Claude Code installed and working — verify with `claude --version`
- Browser access to Zendesk

## Setup

One-time setup — creates the venv, installs dependencies (including the browser
used for cookie capture), and seeds `.env`:

```bash
./setup.sh
# then set ZENDESK_SUBDOMAIN in .env
```

## Running

```bash
./run.sh
```

`run.sh` checks your Zendesk cookie; if it's missing or expired it opens a
browser for you to log in (see [Cookies](#cookies)), captures the cookie, then
starts the app and opens <http://localhost:5001>.

**macOS:** you can also double-click **`Ticket Lens.app`** in Finder — it runs
setup on first launch, then `run.sh`, and stays in the Dock while running
(quit from the Dock to stop it). The first time, macOS may require right-click →
**Open** → **Open**.

## Configuration

### `.env`

- **`ZENDESK_SUBDOMAIN`** — the `YOURCOMPANY` part of `YOURCOMPANY.zendesk.com`.
- **`CLAUDE_CMD`** — usually leave as `claude`. Set to a full path if it isn't on `$PATH`.
- **`CACHE_TTL_SECONDS`** — how long to cache API responses (default 600).
- **`ZENDESK_COOKIE`** — your Zendesk session cookie. Don't set this by hand;
  it's written by `capture_cookies.py` (see [Cookies](#cookies) below).

`.env` holds your session cookie, so it's git-ignored — never commit it.

### Cookies

The Zendesk session cookie lives in `.env` as `ZENDESK_COOKIE`. You don't paste
it by hand — `capture_cookies.py` does it for you:

```bash
.venv/bin/python capture_cookies.py
```

This opens a browser at your Zendesk subdomain. Log in (SSO included); as soon
as it detects an authenticated session (via `/api/v2/users/me.json`) it grabs
the cookie, closes the browser, and writes `ZENDESK_COOKIE` to `.env`.

`run.sh` runs this automatically when needed, so most of the time you won't call
it directly.

**Check whether the current cookie is still valid:**

```bash
.venv/bin/python capture_cookies.py --check
```

Exit code `0` = valid (prints who you're logged in as), `1` = missing/expired,
`2` = couldn't reach Zendesk.

### `customers.json`

Lists the customers you cover. Each entry needs `name`, `slug`, and
`zendesk_org_id`. (The separate Resource Lens app uses a `confluent_org_id`
field in its own `customers.json`.)

## How it works

For each ticket request:
1. Fetches ticket + paginated comments + user details using your cookie
2. Writes the conversation to a temp file
3. Runs `claude -p "<prompt referencing the temp file>"` — Claude Code reads
   the file and produces the brief
4. Caches the result in `cache.db` and renders the UI

No API key, no Anthropic billing — just whatever auth Claude Code already has.

## How sessions expire

Zendesk session cookies typically last hours to a day. When the landing page
shows "Disconnected", the cookie has expired — just re-run `./run.sh` (it
detects the invalid cookie and re-opens the login flow), or run
`.venv/bin/python capture_cookies.py` directly. Because `.env` is read at
startup, restart the app after refreshing the cookie.

## Speed tips

- Bookmark `http://localhost:5001/t/12345` — skips the entry page entirely.
- Briefs and ticket data are cached in `cache.db`. Refresh button (↻) bypasses
  the cache when a ticket has new activity.
- The brief is auto-regenerated when the ticket's `updated_at` is newer than
  the last brief.

## Caveats — read these

- **Policy:** Cookie-scraping bypasses your org's API access process. Confirm
  with your admin that this is OK for your role.
- **Volume:** Don't blast this through hundreds of tickets in a loop. It will
  look like an attack to anyone watching logs, and Zendesk will rate-limit
  (you'll get 429s). The TTL cache helps; keep it on.
- **Claude Code permissions:** The brief prompt explicitly tells Claude not to
  modify any files, but Claude Code's general permissions still apply. The
  temp file written for each request is deleted immediately after.
- **Sensitive data:** Tickets often contain PII. Make sure using Claude Code
  on this content is consistent with your org's policies.
