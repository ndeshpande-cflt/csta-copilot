# CSTA Copilot — Ticket Lens

A small Flask tool that uses your browser session cookie (no API key needed) to
pull Zendesk tickets and generate "30-second briefs" by shelling out to the
Claude Code CLI.

## Prerequisites

- Python 3.9+
- Claude Code installed and working — verify with `claude --version`
- Browser access to Zendesk
- The **Glean MCP server** added to Claude Code (the briefs and chat use it) —
  see below.

### Claude Code: add the Glean MCP server

Briefs and ticket chat enrich answers with internal Glean search, so Claude Code
needs the Glean MCP server configured (once, at user scope so it's available to
every project):

```bash
claude mcp add --transport http glean https://confluent-be.glean.com/mcp/default -s user
```

Verify it's connected:

```bash
claude mcp list            # glean should show "✓ Connected"
claude mcp get glean       # shows URL, transport, scope
```

The first call will prompt you to authenticate to Glean in the browser.

### Claude Code: raise the MCP timeouts

Glean calls can take a while, so give MCP more time than the default. These are
**global** MCP timeouts (they apply to every MCP server, not just Glean), set as
env vars in `~/.claude/settings.json`:

```json
{
  "env": {
    "MCP_TIMEOUT": "300000",
    "MCP_TOOL_TIMEOUT": "300000"
  }
}
```

- **`MCP_TIMEOUT`** — server startup timeout, in milliseconds.
- **`MCP_TOOL_TIMEOUT`** — tool-call timeout, in milliseconds (`300000` = 5 min).

Restart Claude Code (and this app) after changing them.

Useful MCP commands:

```bash
claude mcp list                 # all servers + health
claude mcp get glean            # one server's config
```

## 1. One-time setup

From the `ticket-lens` folder:

```bash
./setup.sh
```

This creates the virtual environment, installs everything, and creates a `.env`.
Then open `.env` and set your Zendesk subdomain:

```
ZENDESK_SUBDOMAIN=yourcompany     # the YOURCOMPANY in YOURCOMPANY.zendesk.com
```

## 2. Run the app

```bash
./run.sh
```

That's it. `run.sh` handles everything else for you:

1. Checks your Zendesk session. **If you're not signed in, a browser window
   opens — just log in (SSO included) and it closes automatically.**
2. Starts the app and opens it at <http://localhost:5001/tickets>.

To stop the app, press **Ctrl+C** in the terminal.

## 3. (macOS) Add a Dock shortcut

For one-click access, put the app in your Dock:

1. In Finder, open the `ticket-lens` folder and find **`Ticket Lens.app`**.
2. Drag it onto your Dock.
3. Click it to launch — it runs setup on first use, signs you in if needed, and
   opens the app. It stays in the Dock with a running indicator while open;
   **right-click → Quit** (or ⌘Q) stops it. Clicking the Dock icon again brings
   the running app's terminal to the front.

> The first time you launch from Finder/Dock, macOS may say it's from an
> "unidentified developer." Right-click the app → **Open** → **Open** once; after
> that it launches normally.

## Configuration

### `customers.json`

Lists the customers you cover. Each entry needs `name`, `slug`, and
`zendesk_org_id`.

## How it works

For each ticket request:
1. Fetches ticket + paginated comments + user details using your cookie
2. Writes the conversation to a temp file
3. Runs `claude -p "<prompt referencing the temp file>"` — Claude Code reads
   the file and produces the brief
4. Caches the result in `cache.db` and renders the UI

No API key, no Anthropic billing — just whatever auth Claude Code already has.

## When your session expires

Zendesk session cookies typically last hours to a day. When the page shows
"Disconnected", your session has expired — just **launch the app again** (Dock
icon or `./run.sh`). It detects the expired session and re-opens the sign-in
browser automatically.

## Speed tips

- Bookmark `http://localhost:5001/t/12345` — skips the entry page entirely.
- Briefs and ticket data are cached in `cache.db`. The refresh button (↻)
  bypasses the cache when a ticket has new activity.
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
