# CSTA Copilot — Resource Lens

A small Flask tool that uses your `admin.confluent.cloud` browser session cookie
(no API key) to browse Confluent Cloud environments, clusters, and their
cloud-resource details (networks, endpoints, placement).

Split out of the Ticket Lens app — the two are independent apps that share only
the `customers.json` shape and the cookie-capture approach.

## Prerequisites

- Python 3.9+
- Browser access to admin.confluent.cloud

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# paste your Confluent Cloud cookie into CONFLUENT_COOKIE in .env
python app.py
# open http://localhost:5002
```

Runs on **port 5002** so it can run alongside Ticket Lens (port 5001).

## Configuration

### `.env`

- **`CONFLUENT_COOKIE`** — your `admin.confluent.cloud` session cookie. Must
  contain `internal_auth_token=<JWT>`. Get it from DevTools → Network → any
  `confluent.cloud/api/internal/…` request → Copy as cURL → paste the cookie
  string (or the whole `-b '...'`).
- **`CACHE_TTL_SECONDS`** — cache window for Confluent Cloud responses (default 600).

### `customers.json`

Each entry needs `name`, `slug`, and `confluent_org_id` (the org whose
environments/clusters you want to browse). Entries without `confluent_org_id`
are shown disabled. Copy the template and fill in your own:

```bash
cp customers.example.json customers.json
```

**`customers.json` is git-ignored** — it holds customer names and org IDs, so it
stays local and is never committed.

## Reference

`cloudresources/` and `metrics/` hold saved `curl` examples of the internal
Confluent Cloud API calls this tool relies on — handy when the undocumented
endpoints change.

## How sessions expire

The `internal_auth_token` JWT in the cookie carries an `exp` claim; the home
page shows time-to-expiry. When it expires, refresh `CONFLUENT_COOKIE` in `.env`
and restart.
