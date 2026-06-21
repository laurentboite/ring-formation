#!/usr/bin/env python3
"""
load_jira_ts.py — Charge les tickets Jira TS (Done) dans jira_tickets.

Rejouable et idempotent : UPSERT sur issue_key, pas de doublon.
salesforce_id résolu via le champ Customer (customfield_12972) → installations.customer.
Si non trouvé, le ticket est quand même inséré avec salesforce_id=NULL.

PRÉREQUIS :
  pip install requests
  export ATLASSIAN_EMAIL=prenom.nom@scality.com
  export ATLASSIAN_TOKEN=<token API Atlassian>

USAGE :
  python3 load_jira_ts.py /chemin/ring_install_base.db
  python3 load_jira_ts.py db.sqlite --since 2025-12-21   # forcer une date
  python3 load_jira_ts.py db.sqlite --full               # tout recharger (6 mois)
  python3 load_jira_ts.py db.sqlite --customer "Kaiser_Permanente"  # un seul client
"""

import os, re, sys, time, sqlite3, datetime, argparse, unicodedata
import urllib.request, urllib.parse, json, base64

CLOUD_ID  = "b25e0fc0-e0f8-4f1e-8bbc-fe383cb8985c"
BASE_URL  = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3"
FIELDS    = [
    "summary", "status", "issuetype", "priority", "created", "updated",
    "resolutiondate", "assignee",
    "customfield_12972",   # Customer
    "customfield_12976",   # Product Version (versions source)
    "customfield_13065",   # OS Version
    "customfield_12502",   # Change type
    "customfield_12973",   # Region
    "customfield_13005",   # Operation Date
    "customfield_13000",   # SFDC Opportunity URL
]

DDL = """
CREATE TABLE IF NOT EXISTS jira_tickets (
    issue_key        TEXT PRIMARY KEY,
    summary          TEXT,
    issue_type       TEXT,
    status           TEXT,
    priority         TEXT,
    created_date     TEXT,
    updated_date     TEXT,
    resolution_date  TEXT,
    assignee         TEXT,
    jira_customer    TEXT,
    salesforce_id    TEXT,
    ring_version_src TEXT,
    os_version       TEXT,
    change_type      TEXT,
    region           TEXT,
    operation_date   TEXT,
    sf_opportunity   TEXT,
    ingested_at      TIMESTAMP
)
"""

# ---------- normalisation pour le matching ----------
def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())

# ---------- client Jira ----------
class Jira:
    def __init__(self):
        email = os.environ["ATLASSIAN_EMAIL"]
        token = os.environ["ATLASSIAN_TOKEN"]
        cred  = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.auth = "Basic " + cred

    def get(self, path, params=None):
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": self.auth, "Accept": "application/json"
        })
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", 60))
                    print(f"  rate limit — attente {wait}s")
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError(f"Échec après 5 tentatives : {path}")

    def search(self, jql, start_at=0, max_results=100):
        return self.get("/search", {
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": ",".join(FIELDS),
        })

# ---------- extraction des champs ----------
def extract(issue):
    f = issue["fields"]

    def first(val):
        if isinstance(val, list) and val:
            return val[0]
        return None

    def sf_opp_id(url):
        if not url:
            return None
        m = re.search(r"/Opportunity/([A-Za-z0-9]{15,18})/", url)
        return m.group(1) if m else None

    return {
        "issue_key":       issue["key"],
        "summary":         f.get("summary"),
        "issue_type":      (f.get("issuetype") or {}).get("name"),
        "status":          (f.get("status") or {}).get("name"),
        "priority":        (f.get("priority") or {}).get("name"),
        "created_date":    (f.get("created") or "")[:10] or None,
        "updated_date":    (f.get("updated") or "")[:10] or None,
        "resolution_date": (f.get("resolutiondate") or "")[:10] or None,
        "assignee":        (f.get("assignee") or {}).get("emailAddress"),
        "jira_customer":   first(f.get("customfield_12972")),
        "ring_version_src":";".join(f.get("customfield_12976") or []) or None,
        "os_version":      first(f.get("customfield_13065")),
        "change_type":     (f.get("customfield_12502") or {}).get("value"),
        "region":          (f.get("customfield_12973") or {}).get("value"),
        "operation_date":  f.get("customfield_13005"),
        "sf_opportunity":  sf_opp_id(f.get("customfield_13000")),
    }

# ---------- résolution salesforce_id ----------
def build_sf_map(cur):
    """Construit un dict norm(customer) → salesforce_id depuis installations."""
    rows = cur.execute(
        "SELECT DISTINCT customer, salesforce_id FROM installations "
        "WHERE customer IS NOT NULL AND salesforce_id IS NOT NULL"
    ).fetchall()
    return {norm(r[0]): r[1] for r in rows}

def resolve_sf(jira_customer, sf_map):
    if not jira_customer:
        return None
    # Le nom Jira utilise des underscores : Kaiser_Permanente → Kaiser Permanente
    candidate = jira_customer.replace("_", " ")
    return sf_map.get(norm(candidate))

# ---------- programme principal ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="Chemin vers la base SQLite")
    ap.add_argument("--since",    help="Date ISO de début (ex: 2025-12-21)")
    ap.add_argument("--full",     action="store_true",
                    help="Recharger depuis 6 mois (ignore sync_history)")
    ap.add_argument("--customer", help="Charger uniquement ce client Jira (ex: Kaiser_Permanente)")
    ap.add_argument("--sleep",    type=float, default=0.2,
                    help="Pause entre pages (défaut: 0.2s)")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.executescript(DDL)
    con.commit()

    sf_map = build_sf_map(cur)
    print(f"Mapping client→SF : {len(sf_map)} entrées")

    # Déterminer la date de départ
    if args.since:
        since = args.since
    elif args.full or args.customer:
        since = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    else:
        row = cur.execute(
            "SELECT sync_date FROM sync_history "
            "WHERE source='jira_ts_load' AND status='ok' "
            "ORDER BY sync_date DESC LIMIT 1"
        ).fetchone()
        if row:
            since = row["sync_date"][:10]
            print(f"Dernier run : {since} — chargement incrémental")
        else:
            since = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
            print(f"Aucun run précédent — chargement depuis {since}")

    # Construction du JQL
    if args.customer:
        jql = (f'project = TS AND "Customer" = "{args.customer}" '
               f'AND statusCategory = Done ORDER BY updated DESC')
    else:
        jql = (f'project = TS AND statusCategory = Done '
               f'AND updated >= "{since}" ORDER BY updated DESC')

    print(f"JQL : {jql}")

    jira      = Jira()
    start_at  = 0
    inserted  = 0
    updated   = 0
    unresolved= 0
    now_ts    = datetime.datetime.now(datetime.timezone.utc).isoformat()

    while True:
        data   = jira.search(jql, start_at=start_at)
        issues = data.get("issues", [])
        total  = data.get("total", 0)

        if not issues:
            break

        for issue in issues:
            t = extract(issue)
            t["salesforce_id"] = resolve_sf(t["jira_customer"], sf_map)
            if not t["salesforce_id"]:
                unresolved += 1

            existing = cur.execute(
                "SELECT 1 FROM jira_tickets WHERE issue_key=?", (t["issue_key"],)
            ).fetchone()

            cur.execute("""
                INSERT INTO jira_tickets
                  (issue_key, summary, issue_type, status, priority,
                   created_date, updated_date, resolution_date, assignee,
                   jira_customer, salesforce_id, ring_version_src, os_version,
                   change_type, region, operation_date, sf_opportunity, ingested_at)
                VALUES
                  (:issue_key,:summary,:issue_type,:status,:priority,
                   :created_date,:updated_date,:resolution_date,:assignee,
                   :jira_customer,:salesforce_id,:ring_version_src,:os_version,
                   :change_type,:region,:operation_date,:sf_opportunity,:ingested_at)
                ON CONFLICT(issue_key) DO UPDATE SET
                  summary=excluded.summary, status=excluded.status,
                  priority=excluded.priority, updated_date=excluded.updated_date,
                  resolution_date=excluded.resolution_date,
                  assignee=excluded.assignee,
                  salesforce_id=COALESCE(excluded.salesforce_id, jira_tickets.salesforce_id),
                  ring_version_src=excluded.ring_version_src,
                  os_version=excluded.os_version, change_type=excluded.change_type,
                  region=excluded.region, operation_date=excluded.operation_date,
                  sf_opportunity=excluded.sf_opportunity,
                  ingested_at=excluded.ingested_at
            """, {**t, "ingested_at": now_ts})

            if existing:
                updated += 1
            else:
                inserted += 1

        con.commit()
        start_at += len(issues)
        print(f"  {start_at}/{total} — insérés={inserted} mis_à_jour={updated} sans_SF={unresolved}")

        if start_at >= total:
            break
        time.sleep(args.sleep)

    # Log dans sync_history
    cur.execute("""
        INSERT INTO sync_history (sync_date, source, records_added, records_updated, status)
        VALUES (?, 'jira_ts_load', ?, ?, 'ok')
    """, (now_ts, inserted, updated))
    # Vérifier si records_updated existe sinon adapter
    con.commit()

    total_in_db = cur.execute("SELECT COUNT(*) FROM jira_tickets").fetchone()[0]
    unres_in_db = cur.execute(
        "SELECT COUNT(*) FROM jira_tickets WHERE salesforce_id IS NULL"
    ).fetchone()[0]

    print(f"\n=== Résultat ===")
    print(f"  Nouveaux insérés : {inserted}")
    print(f"  Mis à jour       : {updated}")
    print(f"  Sans salesforce_id (ce run) : {unresolved}")
    print(f"  Total jira_tickets en base  : {total_in_db}")
    print(f"  Total sans salesforce_id    : {unres_in_db}")
    con.close()

if __name__ == "__main__":
    main()
