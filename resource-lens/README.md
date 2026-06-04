# CSTA Copilot — Resource Lens

A small Flask tool that uses your `admin.confluent.cloud` browser session cookie
(no API key needed) to browse Confluent Cloud environments, clusters, and their
cloud-resource details (networks, endpoints, placement).

Split out of the Ticket Lens app — the two are independent apps that share only
the `customers.json` shape and the cookie-capture approach. Resource Lens runs on
**port 5002** so it can run alongside Ticket Lens (port 5001).

## Prerequisites

- Python 3.9+
- Browser access to `admin.confluent.cloud`

## 1. One-time setup

From the `resource-lens` folder:

```bash
./setup.sh
```

This creates the virtual environment, installs everything (including the
Chromium that the cookie-capture flow uses), and creates a `.env`. There's
nothing to edit afterwards — the cookie is captured for you on first run.

## 2. Run the app

```bash
./run.sh
```

That's it. `run.sh` handles everything else for you:

1. Checks your Confluent Cloud session. **If you're not signed in (or it
   expired), a browser window opens — just log in to `admin.confluent.cloud`
   (SSO included) and it closes automatically once you're authenticated.**
2. Starts the app and opens it at <http://localhost:5002/>.

To stop the app, press **Ctrl+C** in the terminal.

## 3. (macOS) Add a Dock shortcut

For one-click access, put the app in your Dock:

1. Build the app bundle once: `./build_app.sh` (creates **`Resource Lens.app`**).
2. In Finder, open the `resource-lens` folder and drag **`Resource Lens.app`**
   onto your Dock.
3. Click it to launch — it runs setup on first use, signs you in if needed, and
   opens the app. It stays in the Dock with a running indicator while open;
   **right-click → Quit** (or ⌘Q) stops it. Clicking the Dock icon again brings
   the running app's terminal to the front.

> The first time you launch from Finder/Dock, macOS may say it's from an
> "unidentified developer." Right-click the app → **Open** → **Open** once; after
> that it launches normally.

## Configuration

### `.env`

- **`CONFLUENT_COOKIE`** — your `admin.confluent.cloud` session cookie. You
  normally never touch this: `run.sh` captures it via the browser login flow and
  writes it here. It must contain `internal_auth_token=<JWT>`.
- **`CACHE_TTL_SECONDS`** — cache window for Confluent Cloud responses (default 600).

### `customers.json`

Each entry needs `name`, `slug`, and `confluent_org_id` (the org whose
environments/clusters you want to browse). Entries without `confluent_org_id`
are shown disabled. Copy the template and fill in your own:

```bash
cp customers.example.json customers.json
```

**`customers.json` is git-ignored** — it holds customer names and org IDs, so it
stays local and is never committed. Keep your own copy.

## How it works

For each request:
1. Calls the internal `confluent.cloud/api/internal/…` endpoints using your
   captured session cookie
2. Caches the result in `cache.db` and renders the UI

No API key, no service account — just your existing browser session.

`cloudresources/` and `metrics/` hold saved `curl` examples of the internal
Confluent Cloud API calls this tool relies on — handy when the undocumented
endpoints change.

## When your session expires

The `internal_auth_token` JWT in the cookie carries an `exp` claim; the home page
shows time-to-expiry. When it expires, the page shows "Confluent Cloud cookie not
set / Disconnected" — just **launch the app again** (Dock icon or `./run.sh`). It
detects the expired session and re-opens the sign-in browser automatically.

## Caveats — read these

- **Policy:** Cookie-scraping bypasses your org's API access process. Confirm
  with your admin that this is OK for your role.
- **Volume:** Don't blast this through hundreds of orgs/clusters in a loop. It
  will look like an attack to anyone watching logs, and you'll get rate-limited.
  The TTL cache helps; keep it on.
- **Sensitive data:** Org IDs and cluster details are sensitive. Make sure using
  this is consistent with your org's policies. `customers.json` and `.env` are
  git-ignored — keep them local.
