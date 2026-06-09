"""
CSTA Copilot — Resource Lens
----------------------------
Browse Confluent Cloud environments, clusters, and their cloud-resource details
(networks, endpoints, placement) using your browser session cookie — no API key.

Setup:
  1. pip install -r requirements.txt
  2. Set CONFLUENT_COOKIE in .env (your admin.confluent.cloud session cookie)
  3. python app.py
  4. Open http://localhost:5002

This was split out of the Ticket Lens app; the two are independent apps that
share only the customers.json shape and the cookie-capture approach.
"""

import os
import re
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, render_template, request, redirect, url_for

from dotenv import load_dotenv

load_dotenv()

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))  # 10 min default

CUSTOMERS_JSON = Path(__file__).parent / "customers.json"
CLUSTER_METRICS_JSON = Path(__file__).parent / "cluster_metrics.json"
DB_PATH = Path(__file__).parent / "cache.db"

# Placeholders in cluster_metrics.json URLs, replaced per-cluster:
#   #PKCID#  -> physical cluster id (pkc-…, the Grafana k8s_namespace_name)
#   #LKCID#  -> logical cluster id (lkc-…)
#   #LKCNUM# -> logical cluster id without the "lkc-" prefix
#   #ORGID#  -> Confluent org id
METRICS_PKCID_PLACEHOLDER = "#PKCID#"
METRICS_LKCID_PLACEHOLDER = "#LKCID#"
METRICS_LKCNUM_PLACEHOLDER = "#LKCNUM#"
METRICS_ORGID_PLACEHOLDER = "#ORGID#"

app = Flask(__name__)


def _load_customers():
    """Load customer config from customers.json.

    Each entry: {name, slug, id, zendesk_org_id, confluent_org_id}. `id`
    mirrors `zendesk_org_id` for back-compat with the Ticket Lens shape.
    """
    if not CUSTOMERS_JSON.exists():
        return []
    try:
        data = json.loads(CUSTOMERS_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data.get("customers"), list):
        return []
    out = []
    for entry in data["customers"]:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        slug = (entry.get("slug") or "").strip().lower()
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        try:
            zd_int = int(entry["zendesk_org_id"]) if entry.get("zendesk_org_id") is not None else None
        except (TypeError, ValueError):
            zd_int = None
        try:
            cf_int = int(entry["confluent_org_id"]) if entry.get("confluent_org_id") is not None else None
        except (TypeError, ValueError):
            cf_int = None
        if not slug:
            slug = str(zd_int or "")
        out.append({
            "name": name,
            "slug": slug,
            "id": zd_int,                  # back-compat alias
            "zendesk_org_id": zd_int,
            "confluent_org_id": cf_int,
        })
    return out


CUSTOMER_ORGS = _load_customers()


# Groups in cluster_metrics.json that hold link templates. Each item names
# itself with a "metric" or "dashboard" key, plus a "url" with placeholders.
CLUSTER_METRIC_GROUPS = ("metrics", "dashboards")


def _load_cluster_metrics():
    """Load the link templates from cluster_metrics.json, grouped by section
    ("metrics", "dashboards"). Each item is {name, url}; the url carries
    placeholders (#PKCID#, #LKCID#, #LKCNUM#, #ORGID#) filled in per-cluster by
    cluster_metric_links."""
    empty = {g: [] for g in CLUSTER_METRIC_GROUPS}
    if not CLUSTER_METRICS_JSON.exists():
        return empty
    try:
        data = json.loads(CLUSTER_METRICS_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty

    def _section(items):
        out = []
        for m in items or []:
            if not isinstance(m, dict):
                continue
            name = (m.get("metric") or m.get("dashboard") or "").strip()
            url = (m.get("url") or "").strip()
            if name and url:
                out.append({"name": name, "url": url})
        return out

    return {g: _section(data.get(g)) for g in CLUSTER_METRIC_GROUPS}


CLUSTER_METRICS = _load_cluster_metrics()


def _sub_metric_placeholders(url, pkcid, lkcid, orgid):
    lkcnum = lkcid[4:] if lkcid and lkcid.lower().startswith("lkc-") else lkcid
    if pkcid:
        url = url.replace(METRICS_PKCID_PLACEHOLDER, pkcid)
    if lkcid:
        url = url.replace(METRICS_LKCID_PLACEHOLDER, lkcid)
        url = url.replace(METRICS_LKCNUM_PLACEHOLDER, lkcnum)
    if orgid is not None:
        url = url.replace(METRICS_ORGID_PLACEHOLDER, str(orgid))
    return url


def cluster_metric_links(pkcid, lkcid=None, orgid=None):
    """Return {"metrics": [{name, url}], "dashboards": [...]} with the cluster's
    ids substituted into each URL: #PKCID# -> physical id, #LKCID# -> logical id,
    #LKCNUM# -> logical id without the 'lkc-' prefix, #ORGID# -> org id. Groups
    are empty if there's nothing to substitute."""
    result = {g: [] for g in CLUSTER_METRIC_GROUPS}
    if not (pkcid or lkcid or orgid):
        return result
    for g in CLUSTER_METRIC_GROUPS:
        for m in CLUSTER_METRICS.get(g, []):
            result[g].append({
                "name": m["name"],
                "url": _sub_metric_placeholders(m["url"], pkcid, lkcid, orgid),
            })
    return result


def find_org(slug):
    for o in CUSTOMER_ORGS:
        if o["slug"] == slug:
            return o
    return None


@app.context_processor
def _inject_current_user():
    # Resource Lens has no Zendesk identity; templates guard on this being set.
    return {"current_user": None}


# -------------------- Cache / secrets --------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""
          CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            fetched_at INTEGER NOT NULL
          )
        """)
        c.execute("""
          CREATE TABLE IF NOT EXISTS secrets (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
          )
        """)


def cache_get(key, ttl=CACHE_TTL_SECONDS):
    with db() as c:
        row = c.execute(
            "SELECT value, fetched_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    if time.time() - row["fetched_at"] > ttl:
        return None
    return json.loads(row["value"])


def cache_set(key, value):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO cache (key, value, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), int(time.time())),
        )


SECRET_CONFLUENT_COOKIE = "confluent_cloud_cookie"


def secret_get(key):
    try:
        with db() as c:
            row = c.execute(
                "SELECT value FROM secrets WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row["value"] if row else None


def secret_set(key, value):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO secrets (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, int(time.time())),
        )


def confluent_cookie():
    """The admin.confluent.cloud session cookie. Prefer the env var (.env), then
    fall back to anything stored in the DB secrets table."""
    env = (os.getenv("CONFLUENT_COOKIE") or os.getenv("CONFLUENT_CLOUD_COOKIE") or "").strip()
    if env:
        return env
    return (secret_get(SECRET_CONFLUENT_COOKIE) or "").strip()


# -------------------- Cookie status (no network) --------------------

_INTERNAL_AUTH_TOKEN_RE = re.compile(r"internal_auth_token=([^;\s]+)")


def _decode_jwt_payload(token):
    """Decode a JWT payload *without* signature verification — only to inspect
    the `exp` claim for status display. Never trust it for authorization."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        pad = (-len(payload_b64)) % 4
        if pad:
            payload_b64 += "=" * pad
        import base64
        decoded = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
        return json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _humanize_window_seconds(secs):
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60} min"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h" + (f" {m}m" if m else "")
    return f"{secs // 86400}d"


def _fmt_dt(dt):
    if not dt:
        return "—"
    local = dt.astimezone()
    tz = local.strftime("%Z")
    base = local.strftime("%b %-d, %Y %-I:%M %p")
    return f"{base} {tz}".strip()


_ZONE_SUFFIX_RE = re.compile(r"(\d+[a-z])$", re.IGNORECASE)


@app.template_filter("short_zone")
def _short_zone(value):
    """Shorten an availability-zone name to its trailing id, e.g.
    'ap-southeast-1a' -> '1a'. Falls back to the original string if there's no
    such suffix (e.g. an opaque zone id like 'apse1-az2')."""
    if not value:
        return value
    m = _ZONE_SUFFIX_RE.search(str(value))
    return m.group(1) if m else str(value)


@app.template_filter("num")
def _num(value):
    """Format a metric value for display: numbers rounded to the nearest whole
    number with thousands separators, non-numbers as-is, and None as an em dash."""
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{round(f):,}"


@app.template_filter("mb")
def _mb(value):
    """Format a value given in MB: the rounded MB (with thousands separators),
    plus the GB or TB equivalent in brackets when it exceeds 1024 MB."""
    if value is None:
        return "—"
    try:
        mb = float(value)
    except (TypeError, ValueError):
        return str(value)
    out = f"{round(mb):,}"
    if mb > 1024:
        gb = mb / 1024
        out += f" ({gb / 1024:,.2f} TB)" if gb >= 1024 else f" ({gb:,.2f} GB)"
    return out


@app.template_filter("short_date")
def _short_date(value):
    """Render an ISO-8601 timestamp (e.g. '2019-10-21T13:13:17Z') as a friendly
    UTC date like 'Oct 21, 2019'. Falls back to the leading date portion if it
    can't be parsed, and to '' for empty values."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return str(value)[:10]


def _confluent_cookie_status():
    """Return (ok: bool | None, detail: str) for the Confluent Cloud cookie,
    via the embedded internal_auth_token JWT's exp claim (no network call)."""
    cookie = confluent_cookie()
    if not cookie:
        return False, "Not set"
    m = _INTERNAL_AUTH_TOKEN_RE.search(cookie)
    if not m:
        return None, "Cookie set, but no internal_auth_token to verify"
    payload = _decode_jwt_payload(m.group(1))
    if not payload:
        return None, "Cookie set, but auth token can't be decoded"
    exp = payload.get("exp")
    if not exp:
        return None, "Cookie set, but auth token has no exp claim"
    now = int(time.time())
    if exp < now:
        when = datetime.fromtimestamp(exp, tz=timezone.utc).astimezone()
        return False, f"Cookie expired {_fmt_dt(when)}"
    return True, f"Valid · expires in {_humanize_window_seconds(exp - now)}"


# -------------------- Confluent Cloud client --------------------

class MetricLensAuthError(Exception):
    pass


_CFC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0 Safari/537.36"
)


def _cfc_headers():
    return {
        "Cookie": confluent_cookie(),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://admin.confluent.cloud",
        "Referer": "https://admin.confluent.cloud/",
        "User-Agent": _CFC_UA,
    }


def cfc_get(path, ttl=CACHE_TTL_SECONDS, cache_key=None):
    """GET against confluent.cloud internal APIs using the browser session cookie."""
    if not confluent_cookie():
        raise MetricLensAuthError(
            "Missing Confluent Cloud cookie. Set CONFLUENT_COOKIE in .env."
        )
    key = cache_key or f"cfc:{path}"
    cached = cache_get(key, ttl=ttl)
    if cached is not None:
        return cached
    url = f"https://confluent.cloud{path}"
    r = requests.get(url, headers=_cfc_headers(), timeout=60)
    if r.status_code in (401, 403):
        raise MetricLensAuthError(
            f"Confluent Cloud auth failed ({r.status_code}). Your session cookie "
            "likely expired — refresh CONFLUENT_COOKIE in .env."
        )
    if r.status_code == 404:
        raise MetricLensAuthError(
            f"Confluent Cloud returned 404 for {path}. Double-check the org/env id."
        )
    r.raise_for_status()
    data = r.json()
    cache_set(key, data)
    return data


def fetch_environments(confluent_org_id):
    return cfc_get(
        f"/api/internal/organizations/{confluent_org_id}/details?include_deactivated=true"
    )


def cfc_post(path, payload=None, ttl=CACHE_TTL_SECONDS, cache_key=None):
    """POST against confluent.cloud internal APIs using the session cookie.

    With `payload` it sends a JSON body; without, a bodyless POST (some internal
    endpoints take their input as query params)."""
    if not confluent_cookie():
        raise MetricLensAuthError(
            "Missing Confluent Cloud cookie. Set CONFLUENT_COOKIE in .env."
        )
    key = cache_key or f"cfc:POST:{path}:{json.dumps(payload, sort_keys=True)}"
    cached = cache_get(key, ttl=ttl)
    if cached is not None:
        return cached
    url = f"https://confluent.cloud{path}"
    headers = dict(_cfc_headers())
    post_kwargs = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        post_kwargs["json"] = payload
    r = requests.post(url, headers=headers, timeout=60, **post_kwargs)
    if r.status_code in (401, 403):
        raise MetricLensAuthError(
            f"Confluent Cloud auth failed ({r.status_code}). Your session cookie "
            "likely expired — refresh CONFLUENT_COOKIE in .env."
        )
    r.raise_for_status()
    data = r.json()
    cache_set(key, data)
    return data


def fetch_clusters(account_id):
    return cfc_get(f"/api/internal/clusters?account_id={account_id}")


def search_cluster_org(cluster_id):
    """Resolve a logical cluster id (lkc-…) to its organization via the internal
    search endpoint. Returns the `organization` dict, or None if not found.

    POST /api/internal/organizations/search {"cluster_id": …} returns the org
    that owns the cluster (not the environment) — see resolve_cluster for the
    env/cluster lookup that follows.
    """
    try:
        raw = cfc_post(
            "/api/internal/organizations/search",
            {"cluster_id": cluster_id, "include_deactivated": True},
        )
    except requests.HTTPError as e:
        # The search endpoint returns 404 with an `error` body when no cluster
        # matches — treat that as "not found" rather than a hard failure.
        if e.response is not None and e.response.status_code == 404:
            return None
        raise
    if not isinstance(raw, dict) or raw.get("error"):
        return None
    org = raw.get("organization")
    if isinstance(org, dict) and org.get("id"):
        return org
    return None


_LKC_ID_RE = re.compile(r"lkc-[a-z0-9]+", re.IGNORECASE)


def resolve_pkc_to_lkc(pkc_id):
    """Resolve a physical Kafka cluster id (pkc-…) to its logical id (lkc-…) via
    the blast-radius analyze endpoint. Returns the first lkc- id found (in
    document order), or None if there's no match.
    """
    try:
        raw = cfc_post(f"/api/internal/blast-radius/v1/analyze?resources={pkc_id}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise
    found = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            m = _LKC_ID_RE.search(o)
            if m:
                found.append(m.group(0).lower())

    walk(raw)
    return found[0] if found else None


def env_name_for(confluent_org_id, env_id):
    """Best-effort display name for an environment id, via the org's env list.
    Returns None if it can't be resolved."""
    if not confluent_org_id or not env_id:
        return None
    try:
        for e in _extract_environments(fetch_environments(confluent_org_id)):
            if e["id"] == env_id:
                return e["name"]
    except (MetricLensAuthError, requests.RequestException):
        return None
    return None


def resolve_cluster(cluster_id):
    """From just a logical cluster id, find its org, environment, and cluster
    detail. Returns (org_info | None, env_id | None, env_name | None,
    cluster | None).

    The search endpoint only gives us the org, so we then walk that org's
    environments (cached per-env) until we find the cluster. org_info carries the
    discovered org name; if the org matches a customers.json entry we also fill in
    its slug so the breadcrumb can link back into the normal customer flow.
    """
    org = search_cluster_org(cluster_id)
    if not org:
        return None, None, None, None
    org_id = org.get("id")
    info = {"id": org_id, "name": org.get("name") or str(org_id), "slug": None}
    match = next(
        (o for o in CUSTOMER_ORGS if o.get("confluent_org_id") == org_id), None
    )
    if match:
        info["slug"] = match["slug"]
        info["name"] = match["name"]
    try:
        envs = _extract_environments(fetch_environments(org_id))
    except (MetricLensAuthError, requests.RequestException):
        return info, None, None, None
    for env in envs:
        env_id = env["id"]
        try:
            clusters = _extract_clusters(fetch_clusters(env_id))
        except (MetricLensAuthError, requests.RequestException):
            continue
        for c in clusters:
            if c["id"] == cluster_id:
                return info, env_id, env.get("name"), c
    return info, None, None, None


def _extract_environments(raw):
    """Best-effort: pull a list of {id, name, deactivated} from the org-details
    response. Confluent's internal API isn't documented externally, so we try a
    few obvious shapes."""
    candidates = []
    if isinstance(raw, dict):
        for key in ("accounts", "environments", "deployments"):
            val = raw.get(key)
            if isinstance(val, list):
                candidates = val
                break
        if not candidates:
            inner = raw.get("organization") if isinstance(raw.get("organization"), dict) else None
            if inner:
                for key in ("accounts", "environments", "deployments"):
                    val = inner.get(key)
                    if isinstance(val, list):
                        candidates = val
                        break
    out = []
    for e in candidates or []:
        if not isinstance(e, dict):
            continue
        eid = e.get("id") or e.get("account_id") or e.get("resource_id")
        if not eid:
            continue
        out.append({
            "id": str(eid),
            "name": e.get("name") or e.get("display_name") or str(eid),
            "deactivated": bool(e.get("deactivated", False)),
            "created": e.get("created") or "",
        })
    return out


def _extract_clusters(raw):
    """Flatten the nested `/api/internal/clusters` response into per-cluster dicts."""
    candidates = []
    if isinstance(raw, dict):
        for key in ("clusters", "kafka_clusters", "logical_clusters"):
            val = raw.get(key)
            if isinstance(val, list):
                candidates = val
                break
    elif isinstance(raw, list):
        candidates = raw
    out = []
    for entry in candidates or []:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("cluster") if isinstance(entry.get("cluster"), dict) else {}
        lkc = entry.get("id") or inner.get("logical_cluster_id")
        if not (isinstance(lkc, str) and lkc.startswith("lkc-")):
            continue
        placement = inner.get("placement") if isinstance(inner.get("placement"), dict) else {}
        cloud_provider = placement.get("cloud_provider") if isinstance(placement.get("cloud_provider"), dict) else {}
        config = inner.get("config") if isinstance(inner.get("config"), dict) else {}
        zones_raw = cloud_provider.get("zones") or []
        zones = [
            (z.get("name") or z.get("zone_id") or "")
            for z in zones_raw if isinstance(z, dict)
        ]
        zones = [z for z in zones if z]
        out.append({
            "id": lkc,
            "name": entry.get("name") or config.get("name") or lkc,
            "status": inner.get("status") or "",
            "cloud": cloud_provider.get("cloud") or "",
            "region": cloud_provider.get("region") or "",
            "zones": zones,
            "physical_cluster_id": (
                inner.get("physical_cluster_id")
                or placement.get("physical_cluster_id")
                or ""
            ),
            "network_id": inner.get("network_id") or "",
            "network_type": inner.get("selected_network_type") or "",
            "k8s_cluster_id": inner.get("k8s_cluster_id") or "",
            "bootstrap_endpoint": inner.get("bootstrap_endpoint") or "",
            "http_endpoint": inner.get("http_endpoint") or "",
            "created": inner.get("created") or "",
            "modified": inner.get("modified") or "",
            "pending_cku": inner.get("pending_cku"),
            "cku": config.get("cku"),
            "max_network_egress": config.get("max_network_egress"),
            "max_network_ingress": config.get("max_network_ingress"),
            "max_partition": config.get("max_partition"),
            "requested_sla": config.get("requested_sla") or "",
            "sku": config.get("sku") or "",
            "account_id": (
                entry.get("account_id") or config.get("environment") or ""
            ),
        })
    return out


# -------------------- Cluster stats (cloud-obs) --------------------

_OBS_BASE = "https://cloud-obs.aws.cse.confluent.io"


def fetch_cluster_obs(org_id, lkc_id, ttl=CACHE_TTL_SECONDS):
    """Fetch the cloud-obs cluster summary for an org+logical-cluster. This is a
    separate internal host (no Confluent Cloud cookie needed). Returns the parsed
    JSON dict, or None on any failure (so the details page just hides the stats)."""
    if not org_id or not lkc_id:
        return None
    key = f"obs:{org_id}:{lkc_id}"
    cached = cache_get(key, ttl=ttl)
    if cached is not None:
        return cached
    url = f"{_OBS_BASE}/api/kafka/{org_id}/{lkc_id}"
    try:
        r = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "Referer": f"{_OBS_BASE}/resources/{org_id}",
                "User-Agent": _CFC_UA,
            },
            timeout=60,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    cache_set(key, data)
    return data


# Sub-sections shown on the cluster details page, built from the cloud-obs
# "kafka" object. Each is (title, [(label, source_field), …]).
_OBS_SECTIONS = (
    ("Averages", [
        ("Active connections", "avg_active_connection_count"),
        ("Requests / sec", "avg_request_per_second"),
        ("Throughput (MBps)", "avg_thruput_mbps"),
        ("Read (MBps)", "avg_read_mbps"),
        ("Write (MBps)", "avg_write_mbps"),
        ("Storage (GB)", "avg_storage_gb"),
    ]),
    ("Last 24 hours", [
        ("Topics", "l24_num_topic"),
        ("Partitions", "l24_kafka_num_partition"),
        ("Dedicated CKUs", "l24_num_dedicated_cku"),
        ("Avg storage (MB)", "l24_avg_storage_mb"),
        ("Peak active connections", "l24_peak_active_connection_count"),
        ("Peak requests / sec", "l24_peak_request_per_second"),
        ("Peak egress (MBps)", "l24_peak_read_mbps"),
        ("Peak ingress (MBps)", "l24_peak_write_mbps"),
        ("Peak successful auth / sec", "l24_peak_success_auth_attempt_per_second"),
    ]),
    ("Cluster Linking", [
        ("Active links", "cl_num_active_link_count"),
        ("Mirror topics", "cl_num_mirror_topic"),
        ("Source total response (MB)", "cl_source_total_response_mb"),
        ("Destination total response (MB)", "cl_dest_total_response_mb"),
    ]),
)


# Connector columns shown in the Connectors sub-section, read from each item of
# the cloud-obs "connect" array.
_CONNECTOR_FIELDS = (
    "lc_id", "connector_type", "created_at",
    "connect_num_task", "connect_task_hours", "connect_throughput",
)


def _extract_connectors(raw):
    """Pull the displayed columns from the cloud-obs "connect" array (one dict
    per connector). Returns a list of {field: value} dicts."""
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        if isinstance(c, dict):
            out.append({f: c.get(f) for f in _CONNECTOR_FIELDS})
    return out


def _connector_type_counts(connectors):
    """Aggregate connectors by type. Returns [(type, count), …] sorted by count
    (desc) then name."""
    counts = {}
    for c in connectors:
        t = c.get("connector_type") or "Unknown"
        counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def cluster_obs_summary(org_id, lkc_id):
    """Structured cluster stats from cloud-obs for the details page, or None if
    unavailable. Returns {"groups": [{title, rows}, …], "connectors": [{…}, …]}."""
    data = fetch_cluster_obs(org_id, lkc_id)
    if not isinstance(data, dict):
        return None
    kafka = data.get("kafka")
    if not isinstance(kafka, dict):
        return None
    groups = [
        {"title": title, "rows": [(label, kafka.get(field)) for label, field in rows]}
        for title, rows in _OBS_SECTIONS
    ]
    connectors = _extract_connectors(data.get("connect"))
    return {
        "groups": groups,
        "connectors": connectors,
        "connector_types": _connector_type_counts(connectors),
    }


# -------------------- Routes --------------------

def _render_home():
    config_ok = bool(confluent_cookie())
    return render_template(
        "metric_lens_home.html",
        config_ok=config_ok,
        orgs=CUSTOMER_ORGS,
        connected=config_ok,
    )


@app.route("/")
def index():
    return _render_home()


@app.route("/metrics")
def metric_lens_home():
    return _render_home()


_CLUSTER_ID_SEARCH_RE = re.compile(r"((?:lkc|pkc)-[a-z0-9]+)", re.IGNORECASE)


@app.route("/metrics/search", methods=["POST"])
def metric_cluster_search():
    raw = (request.form.get("cluster") or "").strip()
    m = _CLUSTER_ID_SEARCH_RE.search(raw)
    if not m:
        return render_template(
            "error.html",
            message=(
                f"Couldn't read a cluster ID from \"{raw or '(empty)'}\". "
                "Enter a cluster ID like lkc-22xqy or pkc-2or7m."
            ),
        ), 400
    return redirect(url_for("metric_cluster_direct", cluster_id=m.group(1).lower()))


@app.route("/m/<slug>")
def metric_envs(slug):
    org = find_org(slug)
    if not org:
        return render_template("error.html", message=f"Unknown customer: {slug}"), 404
    cf_org_id = org.get("confluent_org_id")
    if not cf_org_id:
        return render_template("error.html", message=(
            f"No confluent_org_id configured for {org['name']} in customers.json. "
            "Add it and reload."
        )), 400
    if request.args.get("refresh") == "1":
        with db() as c:
            c.execute(
                "DELETE FROM cache WHERE key LIKE ?",
                (f"cfc:/api/internal/organizations/{cf_org_id}/details%",),
            )
    try:
        raw = fetch_environments(cf_org_id)
    except MetricLensAuthError as e:
        return render_template("error.html", message=str(e)), 401
    except requests.RequestException as e:
        return render_template("error.html", message=f"Confluent Cloud request failed: {e}"), 502
    envs = _extract_environments(raw)
    return render_template("metric_envs.html", org=org, envs=envs, raw_count=len(envs))


@app.route("/m/<slug>/env/<env_id>")
def metric_clusters(slug, env_id):
    org = find_org(slug)
    if not org:
        return render_template("error.html", message=f"Unknown customer: {slug}"), 404
    if request.args.get("refresh") == "1":
        with db() as c:
            c.execute(
                "DELETE FROM cache WHERE key LIKE ?",
                (f"cfc:/api/internal/clusters?account_id={env_id}%",),
            )
    try:
        raw = fetch_clusters(env_id)
    except MetricLensAuthError as e:
        return render_template("error.html", message=str(e)), 401
    except requests.RequestException as e:
        return render_template("error.html", message=f"Confluent Cloud request failed: {e}"), 502
    clusters = _extract_clusters(raw)
    return render_template(
        "metric_clusters.html", org=org, env_id=env_id, clusters=clusters,
    )


@app.route("/m/<slug>/env/<env_id>/cluster/<cluster_id>")
def metric_cluster(slug, env_id, cluster_id):
    org = find_org(slug)
    if not org:
        return render_template("error.html", message=f"Unknown customer: {slug}"), 404
    org_id = org.get("confluent_org_id") or org.get("id")
    if request.args.get("refresh") == "1":
        with db() as c:
            c.execute(
                "DELETE FROM cache WHERE key LIKE ?",
                (f"cfc:/api/internal/clusters?account_id={env_id}%",),
            )
            c.execute("DELETE FROM cache WHERE key = ?", (f"obs:{org_id}:{cluster_id}",))
    cluster = None
    try:
        raw = fetch_clusters(env_id)
        for c in _extract_clusters(raw):
            if c["id"] == cluster_id:
                cluster = c
                break
    except (MetricLensAuthError, requests.RequestException):
        cluster = None
    metric_links = cluster_metric_links(
        cluster.get("physical_cluster_id"), cluster.get("id"), org_id,
    ) if cluster else {g: [] for g in CLUSTER_METRIC_GROUPS}
    obs = cluster_obs_summary(org_id, cluster["id"]) if cluster else None
    env_name = env_name_for(org.get("confluent_org_id"), env_id)
    return render_template(
        "metric_cluster.html",
        org=org, env_id=env_id, env_name=env_name, cluster_id=cluster_id,
        cluster=cluster, metric_links=metric_links,
        obs_groups=obs["groups"] if obs else None,
        connectors=obs["connectors"] if obs else None,
        connector_types=obs["connector_types"] if obs else None,
    )


@app.route("/m/cluster/<cluster_id>")
def metric_cluster_direct(cluster_id):
    """Direct entry to a cluster's details from the home page's cluster-id
    search. Resolves the cluster's org + environment via the internal search
    endpoint, then renders its details."""
    cluster_id = cluster_id.lower()
    refresh = request.args.get("refresh") == "1"
    if refresh:
        # The id-substring match also covers the cloud-obs cache (obs:<org>:<lkc>)
        # and the org search cache, so a refresh re-fetches both.
        with db() as c:
            c.execute("DELETE FROM cache WHERE key LIKE ?", (f"%{cluster_id}%",))

    # A physical cluster id (pkc-…) is resolved to its logical id first, then we
    # redirect to that lkc- page and follow the normal flow (carry refresh along).
    if cluster_id.startswith("pkc-"):
        try:
            lkc = resolve_pkc_to_lkc(cluster_id)
        except MetricLensAuthError as e:
            return render_template("error.html", message=str(e)), 401
        except requests.RequestException as e:
            return render_template("error.html", message=f"Confluent Cloud request failed: {e}"), 502
        if not lkc:
            return render_template("error.html", message=(
                f"Couldn't resolve {cluster_id} to a logical cluster (lkc-…). "
                "Double-check the physical cluster ID."
            )), 404
        return redirect(url_for("metric_cluster_direct", cluster_id=lkc,
                                **({"refresh": "1"} if refresh else {})))

    try:
        org, env_id, env_name, cluster = resolve_cluster(cluster_id)
    except MetricLensAuthError as e:
        return render_template("error.html", message=str(e)), 401
    except requests.RequestException as e:
        return render_template("error.html", message=f"Confluent Cloud request failed: {e}"), 502
    if not org:
        return render_template("error.html", message=(
            f"Couldn't find {cluster_id} in any organization. Double-check the "
            "cluster ID, or your session may not have access to that org."
        )), 404
    org_id = org.get("confluent_org_id") or org.get("id")
    metric_links = cluster_metric_links(
        cluster.get("physical_cluster_id"), cluster.get("id"), org_id,
    ) if cluster else {g: [] for g in CLUSTER_METRIC_GROUPS}
    obs = cluster_obs_summary(org_id, cluster["id"]) if cluster else None
    return render_template(
        "metric_cluster.html",
        org=org, env_id=env_id, env_name=env_name, cluster_id=cluster_id,
        cluster=cluster, metric_links=metric_links,
        obs_groups=obs["groups"] if obs else None,
        connectors=obs["connectors"] if obs else None,
        connector_types=obs["connector_types"] if obs else None,
    )


@app.route("/settings")
def settings_page():
    # No in-app settings yet — cookies come from CONFLUENT_COOKIE in .env.
    # Endpoint kept so templates' url_for('settings_page') still resolves.
    return redirect(url_for("index"))


# Ensure tables exist on import (cache/secrets used by the cookie resolvers).
init_db()


if __name__ == "__main__":
    print("\n  CSTA Copilot — Resource Lens — http://localhost:5002\n")
    app.run(host="127.0.0.1", port=5002, debug=False)
