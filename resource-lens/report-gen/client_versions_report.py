#!/usr/bin/env python3
"""
client_versions_report.py — list the Kafka client software/versions connecting to
a cluster (from the telemetry Druid "Client Versions" panel) and flag each against
client_versions_compatibility.json as supported / unsupported / unknown.

    ./client_versions_report.py --cluster-id lkc-22xqy

Auth is identical to topic_report.py: the Grafana cookie comes from GRAFANA_COOKIE
in the project .env (captured via capture_grafana_cookie.py); if it's missing or
expired a browser opens to grab a fresh one. Pass --cookie '...' to override, or
--login to force re-capture.

Status rules (per client_software_name + major.minor version):
  * unknown    (amber) — only when the library/family name isn't in the config
  * supported  (green) — version listed with a future EoS date, OR not listed but
                         newer than/equal to the family's oldest tracked version
  * unsupported (red)  — version listed with a past EoS date, OR older than the
                         family's oldest tracked version
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

# Reuse the exact auth + cookie helpers from topic_report.py (same package).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from topic_report import _resolve_grafana_cookie, _cookie_dict  # noqa: E402

HERE = Path(__file__).resolve().parent
for env_path in (HERE / ".env", HERE.parent / ".env"):
    if env_path.exists():
        load_dotenv(env_path)
        break

GRAFANA_URL = (
    "https://grafana.telemetry.aws.confluent.cloud/api/ds/query"
    "?ds_type=confluent-druid-datasource&requestId=SQR127"
)
CONFIG_PATH = HERE / "config" / "client_versions_compatibility.json"

# Status -> (label, css-class). Mirrors the colour scheme requested.
STATUS_SUPPORTED = "supported"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNKNOWN = "unknown"


def fetch_client_versions(cluster_id, cookie, hours=24, debug=False):
    """Query the Druid datasource for client connections, grouped by principal,
    client id, software name and version. Returns (rows, from_ms, to_ms) where
    each row is a dict with principal/client_id/software/version/connections."""
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - hours * 3600 * 1000
    iso = "%Y-%m-%dT%H:%M:%S.000Z"
    interval = (f"{time.strftime(iso, time.gmtime(from_ms / 1000))}/"
                f"{time.strftime(iso, time.gmtime(now_ms / 1000))}")

    sql = (
        'SELECT "user_resource_id" AS "principal", "client_id",\n'
        '  "client_software_name", "client_software_version", '
        'SUM("value_sum") as "connections"\n'
        'FROM "telemetry_metrics_storage"\n'
        f'WHERE __time BETWEEN TIMESTAMPADD(HOUR, -{hours}, CURRENT_TIMESTAMP) '
        'AND CURRENT_TIMESTAMP\n'
        "AND \"name\" IN ('io.confluent.kafka.server/tenant/connection_info_rate')\n"
        f"AND \"tenant\" = '{cluster_id}'\n"
        "GROUP BY 1,2,3, 4\n"
        "ORDER BY 1, 2\n"
        "LIMIT 1001"
    )

    builder = {
        "intervals": {"intervals": [interval], "type": "intervals"},
        "query": sql,
        "queryType": "sql",
    }
    payload = {
        "queries": [
            {
                "builder": builder,
                "datasource": {"type": "confluent-druid-datasource",
                               "uid": "_Pvp4KNVk"},
                "refId": "A",
                "settings": {},
                "expr": json.dumps({"builder": builder, "settings": {}}),
                "datasourceId": 6,
                "intervalMs": 60000,
                "maxDataPoints": 1358,
            }
        ],
        "from": str(from_ms),
        "to": str(now_ms),
    }

    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://grafana.telemetry.aws.confluent.cloud",
        "x-datasource-uid": "_Pvp4KNVk",
        "x-grafana-org-id": "1",
        "x-panel-title": "Client Versions",
        "x-plugin-id": "confluent-druid-datasource",
        "user-agent": "client-versions-report/1.0",
    }

    resp = requests.post(
        GRAFANA_URL, headers=headers, cookies=_cookie_dict(cookie),
        data=json.dumps(payload), timeout=60,
    )
    if resp.status_code in (401, 403):
        sys.exit(
            f"Grafana auth failed ({resp.status_code}). The cookie is likely "
            "expired; re-run with --login to capture a fresh one."
        )
    resp.raise_for_status()
    body = resp.json()
    if debug:
        print(f"[debug] response keys: {list(body.keys())}", file=sys.stderr)
    return _parse_rows(body, debug=debug), from_ms, now_ms


def _parse_rows(body, debug=False):
    """Turn the Druid dataframe response into a list of row dicts."""
    frames = body.get("results", {}).get("A", {}).get("frames", [])
    if debug:
        print(f"[debug] {len(frames)} frame(s) returned", file=sys.stderr)
    rows = []
    for frame in frames:
        fields = frame.get("schema", {}).get("fields", [])
        values = frame.get("data", {}).get("values", [])
        # Map our wanted columns to their field index by (fuzzy) name.
        idx = {}
        for i, f in enumerate(fields):
            n = (f.get("name") or "").lower()
            if n in ("principal", "user_resource_id"):
                idx["principal"] = i
            elif n == "client_id":
                idx["client_id"] = i
            elif n == "client_software_name":
                idx["software"] = i
            elif n == "client_software_version":
                idx["version"] = i
            elif n == "connections":
                idx["connections"] = i
        if "software" not in idx or "version" not in idx:
            continue
        n_rows = len(values[idx["software"]]) if values else 0
        for r in range(n_rows):
            def col(key, default=""):
                j = idx.get(key)
                return values[j][r] if j is not None and r < len(values[j]) else default
            conn = col("connections", 0)
            rows.append({
                "principal": col("principal"),
                "client_id": col("client_id"),
                "software": col("software"),
                "version": col("version"),
                "connections": conn if isinstance(conn, (int, float)) else 0,
            })
    return rows


def load_config(path=CONFIG_PATH):
    if not path.exists():
        sys.exit(f"Config not found: {path}")
    return json.loads(path.read_text())


def _normalize_version(family, version):
    """Map a reported version onto the family's own numbering scheme.

    Confluent-built Kafka Java clients carry a '-ccs', '-cce' or '-ce' suffix and
    use the Confluent Platform major version (CP 7.x == Apache Kafka 3.x), so we
    subtract 4 from the major: '7.4.0-ccs' -> '3.4.0', '8.4.0-0-0-ce' -> '4.4.0-0-0-ce'.
    Everything else passes through."""
    v = str(version).strip()
    if family == "apache-kafka-java" and re.search(r"-(?:ccs|cce|ce)$", v, re.I):
        m = re.match(r"^(\d+)(\.\d+.*)$", v)
        if m:
            return f"{int(m.group(1)) - 4}{m.group(2)}"
    return v


def _version_tuple(version):
    """'3.5.1' / '3.5.x' -> (3, 5); None if it doesn't look like X.Y[...]."""
    m = re.match(r"^(\d+)\.(\d+)", str(version).strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _major_minor_key(version):
    """'3.5.1' -> '3.5.x'; returns None if it doesn't look like X.Y[...]."""
    vt = _version_tuple(version)
    return f"{vt[0]}.{vt[1]}.x" if vt else None


def _parse_eos(eos_str):
    """'17-02-2029' -> date(2029, 2, 17); None if unparseable."""
    try:
        d, mth, y = (int(x) for x in eos_str.split("-"))
        return date(y, mth, d)
    except (ValueError, AttributeError):
        return None


def classify(software, version, config, today=None):
    """Return (status, eos_str, config_key). config_key is the matched 'X.Y.x'.

    Software names are resolved through the config's "_aliases" map first, so
    librdkafka-family wrappers (confluent-kafka-go/-dotnet/-python) fall back to
    the "librdkafka" support schedule (their versions track librdkafka's).

    'unknown' is returned ONLY when the library/family name doesn't match the
    config. Once the family is known:
      * if the exact major.minor is listed -> supported/unsupported by its EoS date
      * otherwise compare to the family's minimum tracked version: older than the
        minimum -> unsupported; newer/unlisted -> supported.

    Confluent-built Java client versions ('7.4.0-ccs') are normalised to their
    Apache Kafka equivalent ('3.4.0') before evaluation.
    """
    today = today or date.today()
    family = config.get("_aliases", {}).get(software, software)
    versions = config.get(family)
    if not versions:
        return STATUS_UNKNOWN, "", ""

    version = _normalize_version(family, version)
    key = _major_minor_key(version)
    eos_str = versions.get(key) if key else None
    if eos_str:
        eos = _parse_eos(eos_str)
        if eos is not None:
            status = STATUS_SUPPORTED if eos >= today else STATUS_UNSUPPORTED
            return status, eos_str, key

    # Family is known but this exact version isn't listed (or its date is
    # unparseable). Compare against the oldest tracked version in the family.
    vt = _version_tuple(version)
    fam_tuples = [t for t in (_version_tuple(k) for k in versions) if t]
    if vt is not None and fam_tuples and vt < min(fam_tuples):
        return STATUS_UNSUPPORTED, eos_str or "", key or ""
    return STATUS_SUPPORTED, eos_str or "", key or ""


def _format_range(from_ms, to_ms, hours):
    if from_ms is None or to_ms is None:
        return f"last {hours}h"
    fmt = "%Y-%m-%d %H:%M"
    start = time.strftime(fmt, time.localtime(from_ms / 1000))
    end = time.strftime(fmt + " %Z", time.localtime(to_ms / 1000))
    return f"{start} → {end} (last {hours}h)"


def _render_html(cluster_id, hours, rows, counts, from_ms, to_ms):
    generated = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    range_str = _format_range(from_ms, to_ms, hours)

    body_rows = []
    for r in rows:
        st = r["status"]
        body_rows.append(
            "      <tr>"
            f"<td>{html.escape(str(r['principal']))}</td>"
            f"<td>{html.escape(str(r['client_id']))}</td>"
            f"<td>{html.escape(str(r['software']))}</td>"
            f"<td>{html.escape(str(r['version']))}</td>"
            f"<td class='num'>{int(r['connections']):,}</td>"
            f"<td>{html.escape(r['eos']) if r['eos'] else '—'}</td>"
            f"<td><span class='badge {st}'>{st}</span></td>"
            "</tr>"
        )
    rows_html = "\n".join(body_rows)

    # Aggregate clients by unique (software, version) within a status, so each
    # support category gets its own "by library & version" table.
    def _aggregate(status):
        agg = {}
        for r in rows:
            if r["status"] != status:
                continue
            k = (str(r["software"]), str(r["version"]))
            a = agg.setdefault(k, {"software": k[0], "version": k[1],
                                   "eos": r["eos"], "clients": 0, "connections": 0})
            a["clients"] += 1
            a["connections"] += r["connections"]
        return sorted(agg.values(),
                      key=lambda a: (-a["connections"], a["software"], a["version"]))

    def _agg_block(status, label):
        agg_rows = _aggregate(status)
        if not agg_rows:
            return ""
        agg_body = "\n".join(
            "          <tr>"
            f"<td>{html.escape(a['software'])}</td>"
            f"<td>{html.escape(a['version'])}</td>"
            f"<td>{html.escape(a['eos']) if a['eos'] else '—'}</td>"
            f"<td class='num'>{a['clients']:,}</td>"
            f"<td class='num'>{int(a['connections']):,}</td>"
            "</tr>"
            for a in agg_rows
        )
        return f"""    <div class="agg-block">
      <h2 class="section-title">{label} clients — by library &amp; version
        <span class="muted">({len(agg_rows)} unique, {sum(a['clients'] for a in agg_rows)} clients)</span></h2>
      <table class="agg {status}">
        <thead>
          <tr><th>Software</th><th>Version</th><th>End of Support</th>
              <th class="num">Clients</th><th class="num">Connections</th></tr>
        </thead>
        <tbody>
{agg_body}
        </tbody>
      </table>
    </div>
"""

    agg_blocks = (
        _agg_block(STATUS_UNSUPPORTED, "Unsupported")
        + _agg_block(STATUS_SUPPORTED, "Supported")
        + _agg_block(STATUS_UNKNOWN, "Unknown")
    )
    agg_section = f'  <div class="agg-row">\n{agg_blocks}  </div>\n' if agg_blocks else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Client versions — {html.escape(cluster_id)}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 2rem; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 .25rem; }}
  .meta {{ color: #666; font-size: .85rem; margin-bottom: 1rem; }}
  .meta strong {{ color: #444; }}
  .summary {{ margin-bottom: 1.25rem; font-size: .9rem; }}
  table {{ border-collapse: collapse; width: 100%; max-width: 1100px;
          background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th, td {{ padding: .5rem .75rem; border-bottom: 1px solid #eee; text-align: left;
           font-family: ui-monospace, Menlo, monospace; font-size: .88rem; }}
  th {{ background: #f0f3f7; font-size: .78rem; text-transform: uppercase;
       letter-spacing: .04em; color: #555; font-family: inherit; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr:hover td {{ background: #f7faff; }}
  .badge {{ display: inline-block; padding: .15rem .5rem; border-radius: 10px;
           font-size: .75rem; font-weight: 600; text-transform: capitalize;
           font-family: -apple-system, sans-serif; }}
  .badge.supported   {{ background: #e8f5e9; color: #2e7d32; }}
  .badge.unsupported {{ background: #ffebee; color: #c62828; }}
  .badge.unknown     {{ background: #fff8e1; color: #f9a825; }}
  .pill {{ display: inline-block; padding: .1rem .55rem; border-radius: 10px;
          font-weight: 600; margin-right: .4rem; }}
  .pill.supported   {{ background: #e8f5e9; color: #2e7d32; }}
  .pill.unsupported {{ background: #ffebee; color: #c62828; }}
  .pill.unknown     {{ background: #fff8e1; color: #f9a825; }}
  .section-title {{ font-size: 1rem; margin: 1.5rem 0 .5rem; }}
  .section-title .muted {{ color: #999; font-weight: 400; font-size: .85rem; }}
  .agg-row {{ display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: flex-start;
             margin: 1rem 0; }}
  .agg-block {{ flex: 1 1 340px; min-width: 300px; max-width: 560px; }}
  .agg-block .section-title {{ margin-top: 0; }}
  table.agg {{ max-width: 100%; border-left: 3px solid #ccc; }}
  table.agg.unsupported {{ border-left-color: #c62828; }}
  table.agg.supported   {{ border-left-color: #2e7d32; }}
  table.agg.unknown     {{ border-left-color: #f9a825; }}
</style>
</head>
<body>
  <h1>Kafka client versions — {html.escape(cluster_id)}</h1>
  <div class="meta">
    <div><strong>Time range:</strong> {html.escape(range_str)}</div>
    <div><strong>Generated:</strong> {html.escape(generated)}</div>
    <div><strong>Clients:</strong> {len(rows)}</div>
    <div><a href="https://docs.confluent.io/cloud/current/client-apps/overview.html#client-versions-and-support"
           target="_blank" rel="noopener">Client version support matrix &#8599;</a></div>
  </div>
  <div class="summary">
    <span class="pill supported">{counts[STATUS_SUPPORTED]} supported</span>
    <span class="pill unsupported">{counts[STATUS_UNSUPPORTED]} unsupported</span>
    <span class="pill unknown">{counts[STATUS_UNKNOWN]} unknown</span>
  </div>
{agg_section}
  <h2 class="section-title">All clients <span class="muted">({len(rows)})</span></h2>
  <table>
    <thead>
      <tr><th>Principal</th><th>Client ID</th><th>Software</th><th>Version</th>
          <th class="num">Connections</th><th>End of Support</th><th>Status</th></tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(
        description="Report Kafka client versions for a cluster and flag support status."
    )
    parser.add_argument("--cluster-id", required=True, help="Kafka cluster id (lkc-...)")
    parser.add_argument("--cookie", default=None,
                        help="Grafana cookie string (defaults to GRAFANA_COOKIE in .env)")
    parser.add_argument("--login", action="store_true",
                        help="Force the browser login flow to (re)capture the Grafana cookie")
    parser.add_argument("--hours", type=int, default=24,
                        help="Look-back window in hours (default 24)")
    parser.add_argument("--debug", action="store_true",
                        help="Print the Grafana response structure to stderr")
    args = parser.parse_args()

    if not args.cluster_id.startswith("lkc-"):
        sys.exit("--cluster-id must start with 'lkc-'")

    grafana_cookie = _resolve_grafana_cookie(args)
    if not grafana_cookie:
        sys.exit("No Grafana cookie available — capture aborted.")

    config = load_config()

    print(f"Fetching client versions for {args.cluster_id} (last {args.hours}h)...")
    rows, from_ms, to_ms = fetch_client_versions(
        args.cluster_id, grafana_cookie, hours=args.hours, debug=args.debug)
    if not rows:
        print("No client data returned. Check the cluster id / cookie / time window.")
        return

    today = date.today()
    counts = {STATUS_SUPPORTED: 0, STATUS_UNSUPPORTED: 0, STATUS_UNKNOWN: 0}
    for r in rows:
        status, eos, key = classify(r["software"], r["version"], config, today)
        r["status"], r["eos"], r["config_key"] = status, eos, key
        counts[status] += 1

    # Sort: unsupported first, then unknown, then supported; ties by connections.
    order = {STATUS_UNSUPPORTED: 0, STATUS_UNKNOWN: 1, STATUS_SUPPORTED: 2}
    rows.sort(key=lambda r: (order[r["status"]], -r["connections"]))

    # Console summary.
    sw_w = min(max((len(str(r["software"])) for r in rows), default=8), 28)
    print(f"\n{'SOFTWARE':<{sw_w}}  {'VERSION':<10}  {'STATUS':<12}  {'END OF SUPPORT':<14}")
    print(f"{'-' * sw_w}  {'-' * 10}  {'-' * 12}  {'-' * 14}")
    for r in rows:
        sw = str(r["software"])[:sw_w]
        print(f"{sw:<{sw_w}}  {str(r['version']):<10}  {r['status']:<12}  "
              f"{r['eos'] or '—':<14}")
    print(f"\n{len(rows)} clients — {counts[STATUS_SUPPORTED]} supported, "
          f"{counts[STATUS_UNSUPPORTED]} unsupported, {counts[STATUS_UNKNOWN]} unknown.")

    out_dir = HERE / "reports-output" / "client_versions"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.cluster_id}.html"
    out_path.write_text(_render_html(args.cluster_id, args.hours, rows, counts,
                                     from_ms, to_ms))
    print(f"\nSaved report to {out_path}")


if __name__ == "__main__":
    main()
