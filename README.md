# CSTA Copilot

Tools that help a CSTA (Customer Success Technical Architect) prep faster, using
your existing browser session cookies — **no API keys**. Each tool is a small,
self-contained Flask app; pick the one you need.

## The apps

| App | What it does | Port | Docs |
|-----|--------------|------|------|
| **[Ticket Lens](ticket-lens/)** | Pulls Zendesk tickets and generates "30-second briefs" (and ticket chat) by shelling out to the Claude Code CLI, enriched with Glean search. | 5001 | [ticket-lens/README.md](ticket-lens/README.md) |
| **[Resource Lens](resource-lens/)** | Browses Confluent Cloud environments, clusters, and their cloud-resource details (networks, endpoints, placement) via the internal admin API. | 5002 | [resource-lens/README.md](resource-lens/README.md) |

They run independently and on different ports, so you can use both at once.

### Ticket Lens

Sign in to Zendesk once (a browser window opens automatically), then browse your
customers' active tickets and open any ticket to get an AI-generated brief —
sentiment, what's happening, and what to do next — plus a chat to ask follow-up
questions. Uses your Zendesk session cookie and the local Claude Code CLI; no
Anthropic billing. Needs the **Glean MCP server** configured in Claude Code.

→ See [ticket-lens/README.md](ticket-lens/README.md) for setup, the one-command
run flow, and the macOS Dock app.

### Resource Lens

Point it at a customer's Confluent org and browse their environments and Kafka
clusters, drilling into per-cluster cloud details. Uses your
`admin.confluent.cloud` session cookie.

→ See [resource-lens/README.md](resource-lens/README.md) for setup.

## Quick start

Each app sets up and runs from its own folder:

```bash
cd ticket-lens     # or: cd resource-lens
./setup.sh         # one-time: venv, dependencies, .env  (Ticket Lens)
./run.sh           # start the app and open it in your browser
```

> The `setup.sh` / `run.sh` / macOS Dock-app convenience wrappers exist for
> Ticket Lens. Resource Lens currently uses the manual setup in its README
> (venv + `.env`).

## Shared conventions

- **No API keys.** Both apps authenticate with the session cookie from your
  logged-in browser; the value lives in each app's `.env` and is git-ignored.
- **`customers.json`.** Each app keeps its own list of the customers you cover.
  Ticket Lens keys on `zendesk_org_id`; Resource Lens on `confluent_org_id`.
- **`cache.db`.** Each app caches API responses in a local SQLite file
  (git-ignored).

## Repository layout

```
csta-copilot/
├── ticket-lens/      # Zendesk ticket briefs (Flask app, port 5001)
└── resource-lens/    # Confluent Cloud resource browser (Flask app, port 5002)
```

## Caveats

Cookie-based access bypasses your org's normal API process, tickets and cluster
data often contain sensitive/PII data, and Zendesk will rate-limit aggressive
use. See each app's README for the full list before sharing or scripting these.
