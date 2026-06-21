#!/usr/bin/env python3
"""
build_rd_zd_map.py — Construit/enrichit la table rd_customers
en corrélant les tickets RD Jira ↔ tickets ZD Zendesk.

Deux directions de scan complémentaires :
  --from-zd  : cherche les tickets ZD mentionnant "RD-" dans les commentaires
               → extrait les RD keys → rattache via org_to_salesforce
  --from-jira: parcourt les RD Jira (description + commentaires) pour trouver
               des références à des tickets ZD (ID ou URL)
               → look-up dans zendesk_tickets → rattache via org_to_salesforce
  (défaut : les deux directions)

Options supplémentaires :
  --only-open       ne traite que les RD Jira de statut non-Done (alerte précoce)
  --since DATE      ne traite que les tickets ZD/RD mis à jour depuis DATE (YYYY-MM-DD)
  --report FILE     exporte en CSV les liens trouvés non résolus (org sans salesforce_id)
  --dry-run         affiche sans écrire en base

Prérequis :
  Variables d'env JIRA :
    JIRA_BASE_URL  ex. https://jira.scality.com
    JIRA_EMAIL     (compte de service ou perso)
    JIRA_API_TOKEN (token API Jira)
  Variables d'env ZD :
    ZENDESK_SUBDOMAIN  ex. scality-support
    ZENDESK_EMAIL
    ZENDESK_API_TOKEN

Le script N'ÉCRASE JAMAIS une ligne déjà présente en base (INSERT OR IGNORE).
La table rd_customers est la cible ; org_to_salesforce sert de pivot.
"""

import os, re, sys, csv, json, time, sqlite3, datetime, argparse, base64, urllib.parse, urllib.request

# ---------- patterns de détection ----------

RE_RD = re.compile(r'\bRD-(\d+)\b', re.IGNORECASE)

# Références ZD dans du texte Jira :
# "#38004", "ZD#38004", "ZD-38004", "ZD 38004",
# "zendesk.com/agent/tickets/38004", "/hc/requests/38004", "ZD:38004"
RE_ZD_REF = re.compile(
    r'(?:'
    r'zendesk\.com/(?:agent/)?tickets?/(\d{4,6})'
    r'|/hc/(?:requests|tickets)/(\d{4,6})'
    r'|(?:ZD|zd)[#:\s\-]+(\d{4,6})'
    r'|(?<!\d)#(\d{5,6})(?!\d)'   # "#38004" isolé (min 5 chiffres pour éviter PR/CR)
    r')',
    re.IGNORECASE
)

def zd_ids_from_text(text):
    """Extrait tous les IDs ZD (int) d'un texte Jira."""
    ids = set()
    for m in RE_ZD_REF.finditer(text or ''):
        raw = next(g for g in m.groups() if g)
        ids.add(int(raw))
    return ids

def rd_keys_from_text(text):
    """Extrait tous les RD-NNNN d'un texte ZD."""
    return {f"RD-{m.group(1)}" for m in RE_RD.finditer(text or '')}

# ---------- client Jira ----------

class Jira:
    def __init__(self):
        self.base = os.environ['JIRA_BASE_URL'].rstrip('/')
        email = os.environ['JIRA_EMAIL']
        token = os.environ['JIRA_API_TOKEN']
        cred = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.auth = "Basic " + cred

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            'Authorization': self.auth, 'Accept': 'application/json'})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get('Retry-After', 60))
                    print(f"    Jira rate-limit, attente {wait}s…"); time.sleep(wait); continue
                if e.code in (401, 403):
                    raise RuntimeError(f"Jira auth error {e.code} — vérifier JIRA_EMAIL / JIRA_API_TOKEN")
                raise
            except Exception as e:
                if attempt == 4: raise
                time.sleep(2 ** attempt)
        return {}

    def search_issues(self, jql, fields=None, start=0, max_results=50):
        params = {'jql': jql, 'startAt': start, 'maxResults': max_results}
        if fields:
            params['fields'] = ','.join(fields)
        return self._get('/rest/api/2/search', params)

    def get_comments(self, issue_key):
        data = self._get(f'/rest/api/2/issue/{issue_key}/comment',
                         {'maxResults': 200})
        return data.get('comments', [])

    def get_issue(self, issue_key, fields=None):
        params = {}
        if fields:
            params['fields'] = ','.join(fields)
        return self._get(f'/rest/api/2/issue/{issue_key}', params)

    def iter_rd_issues(self, only_open=False, since=None, batch=100):
        """Itère sur tous les RD (ou seulement ouverts / récents)."""
        clauses = ['project = RD', 'issuetype != Test']
        if only_open:
            clauses.append('statusCategory != Done')
        if since:
            clauses.append(f'updated >= "{since}"')
        jql = ' AND '.join(clauses) + ' ORDER BY updated DESC'
        start = 0
        while True:
            data = self.search_issues(jql, fields=['summary', 'status', 'description', 'updated'],
                                      start=start, max_results=batch)
            issues = data.get('issues', [])
            if not issues:
                break
            yield from issues
            start += len(issues)
            if start >= data.get('total', 0):
                break
            time.sleep(0.3)

# ---------- client Zendesk ----------

class Zendesk:
    def __init__(self):
        sub = os.environ['ZENDESK_SUBDOMAIN']
        self.base = f"https://{sub}.zendesk.com/api/v2"
        email = os.environ['ZENDESK_EMAIL']
        token = os.environ['ZENDESK_API_TOKEN']
        cred = base64.b64encode(f"{email}/token:{token}".encode()).decode()
        self.auth = "Basic " + cred

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            'Authorization': self.auth, 'Accept': 'application/json'})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get('Retry-After', 60))
                    print(f"    ZD rate-limit, attente {wait}s…"); time.sleep(wait); continue
                if e.code in (401, 403):
                    raise RuntimeError(f"ZD auth error {e.code} — vérifier ZENDESK_EMAIL / ZENDESK_API_TOKEN")
                raise
            except Exception as e:
                if attempt == 4: raise
                time.sleep(2 ** attempt)
        return {}

    def get_comments(self, ticket_id):
        data = self._get(f'/tickets/{ticket_id}/comments', {'per_page': 100})
        return data.get('comments', [])

    def search_tickets(self, query, page=1, per_page=100):
        params = {'query': query, 'page': page, 'per_page': per_page}
        return self._get('/search.json', params)

    def iter_rd_tickets(self, since=None, batch=100):
        """Cherche tous les tickets ZD mentionnant 'RD-' mis à jour depuis `since`."""
        since_clause = f' updated>{since}' if since else ''
        query = f'tags:ring "RD-"{since_clause} type:ticket'
        page = 1
        while True:
            data = self.search_tickets(query, page=page, per_page=batch)
            tickets = data.get('results', [])
            if not tickets:
                break
            yield from tickets
            if not data.get('next_page'):
                break
            page += 1
            time.sleep(0.3)

# ---------- base de données ----------

def ensure_columns(cur):
    """Ajoute les colonnes manquantes à rd_customers."""
    cur.execute("PRAGMA table_info(rd_customers)")
    existing = {r[1] for r in cur.fetchall()}
    # colonnes utiles non forcément présentes dans les vieilles versions de la table
    extras = [
        ("link_source",     "TEXT"),
        ("ring_version_tag","TEXT"),
        ("priority",        "TEXT"),
        ("organization_id", "TEXT"),
        ("salesforce_id",   "TEXT"),
        ("customer",        "TEXT"),
        ("ingested_at",     "TIMESTAMP"),
    ]
    for col, typ in extras:
        if col not in existing:
            cur.execute(f"ALTER TABLE rd_customers ADD COLUMN {col} {typ}")

def upsert_rd_customer(cur, rd_key, zd_ticket_id, org_id, salesforce_id,
                       customer, ring_version, priority, source, now, dry_run):
    """INSERT OR IGNORE dans rd_customers ; retourne True si nouvelle ligne."""
    if dry_run:
        print(f"    [DRY] {rd_key} ↔ ZD#{zd_ticket_id} org={org_id} sf={salesforce_id}")
        return True
    cur.execute("""
        INSERT OR IGNORE INTO rd_customers
          (rd_key, zendesk_ticket, organization_id, salesforce_id, customer,
           ring_version_tag, priority, link_source, ingested_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (rd_key, zd_ticket_id, org_id, salesforce_id, customer,
          ring_version, priority, source, now))
    return cur.rowcount > 0

def resolve_sf(cur, org_id):
    """Retourne (salesforce_id, customer) depuis org_to_salesforce, ou (None, None)."""
    if not org_id:
        return None, None
    row = cur.execute(
        "SELECT salesforce_id, customer FROM org_to_salesforce WHERE organization_id=?",
        (str(org_id),)).fetchone()
    if row:
        return row[0], row[1]
    return None, None

def version_from_tags(tags):
    """Extrait la version RING depuis les tags ZD (ring_version_X_Y_Z_W)."""
    if not tags:
        return None
    re_tag = re.compile(r'^ring_version_(\d+)_(\d+)_(\d+)_(\d+)$')
    for t in tags:
        m = re_tag.match(t)
        if m:
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}.{m.group(4)}"
    return None

# ---------- direction ZD → Jira ----------

def scan_from_zd(jira_client, zd_client, cur, known_rds, since, dry_run, sleep):
    """
    Parcourt les tickets ZD mentionnant 'RD-' (depuis `since` si fourni),
    lit leurs commentaires, extrait les RD keys, insère dans rd_customers.
    Retourne le nombre de nouveaux liens.
    """
    inserted = 0
    new_rds_found = 0

    # 1. Commencer par les tickets déjà en base avec tag ring+RD
    cur.execute("""
        SELECT zt.id, zt.organization_id, zt.tags, zt.priority, zt.description
        FROM zendesk_tickets zt
        WHERE (zt.tags LIKE '%RD-%' OR zt.description LIKE '%RD-%')
        """ + (f"AND zt.updated_date >= '{since}'" if since else "") + """
        ORDER BY zt.updated_date DESC
    """)
    db_tickets = cur.fetchall()
    print(f"  ZD→Jira : {len(db_tickets)} tickets en base avec refs RD potentielles")

    for row in db_tickets:
        zd_id, org_id, tags_str, priority, desc = row
        tags = json.loads(tags_str) if tags_str and tags_str.startswith('[') else \
               (tags_str.split(',') if tags_str else [])
        ring_ver = version_from_tags(tags)
        sf_id, customer = resolve_sf(cur, org_id)

        # Cherche les RD refs dans la description (déjà en base)
        rd_refs = rd_keys_from_text(desc)

        # Fetch commentaires pour plus de refs
        try:
            comments = zd_client.get_comments(zd_id)
            for c in comments:
                rd_refs |= rd_keys_from_text(c.get('body', ''))
            time.sleep(sleep)
        except Exception as e:
            print(f"    WARN ZD#{zd_id}: {e}")

        for rd_key in rd_refs:
            if rd_key not in known_rds:
                # RD inconnu — on l'ajoute à rd_issues si possible
                new_rds_found += 1
                try:
                    issue = jira_client.get_issue(rd_key, fields=['summary', 'status', 'description'])
                    summary = issue['fields']['summary']
                    status = issue['fields']['status']['name']
                    if not dry_run:
                        cur.execute("""INSERT OR IGNORE INTO rd_issues(issue_key, summary, status)
                                       VALUES(?,?,?)""", (rd_key, summary, status))
                    known_rds.add(rd_key)
                    print(f"    Nouveau RD découvert : {rd_key} — {summary[:60]}")
                except Exception as e:
                    print(f"    WARN cannot fetch {rd_key}: {e}")
                    continue

            ok = upsert_rd_customer(cur, rd_key, zd_id, str(org_id) if org_id else None,
                                    sf_id, customer, ring_ver, priority, 'zd_comment_scan',
                                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                    dry_run)
            if ok:
                inserted += 1

    print(f"  ZD→Jira : {inserted} nouveaux liens, {new_rds_found} nouveaux RD découverts")
    return inserted

# ---------- direction Jira → ZD ----------

def scan_from_jira(jira_client, cur, known_zd_ids, since, only_open, dry_run, sleep):
    """
    Parcourt les RD Jira (description + commentaires),
    extrait les références à des tickets ZD,
    les cherche dans zendesk_tickets ou org_to_salesforce,
    insère dans rd_customers.
    Retourne le nombre de nouveaux liens.
    """
    inserted = 0
    not_in_db = 0

    for i, issue in enumerate(jira_client.iter_rd_issues(only_open=only_open, since=since)):
        rd_key = issue['key']
        fields = issue['fields']
        summary = fields.get('summary', '')
        status = fields.get('status', {}).get('name', '')
        desc = fields.get('description') or ''

        # Mise à jour rd_issues si le statut a changé
        if not dry_run:
            cur.execute("""INSERT OR IGNORE INTO rd_issues(issue_key, summary, status)
                           VALUES(?,?,?)
                           ON CONFLICT(issue_key) DO UPDATE SET
                             summary=excluded.summary, status=excluded.status
                           WHERE rd_issues.status != excluded.status""",
                        (rd_key, summary, status))

        # Cherche les refs ZD dans la description + commentaires
        zd_refs = zd_ids_from_text(desc)
        try:
            for comment in jira_client.get_comments(rd_key):
                zd_refs |= zd_ids_from_text(comment.get('body', ''))
            time.sleep(sleep)
        except Exception as e:
            print(f"  WARN {rd_key} comments: {e}")

        if not zd_refs:
            continue

        for zd_id in zd_refs:
            # Cherche le ticket dans la base locale
            row = cur.execute("""SELECT id, organization_id, tags, priority
                                 FROM zendesk_tickets WHERE id=?""",
                              (zd_id,)).fetchone()
            if not row:
                not_in_db += 1
                # On insère quand même avec org_id inconnu (le ticket est peut-être trop récent)
                ok = upsert_rd_customer(cur, rd_key, zd_id, None, None, None, None, None,
                                        'jira_comment_scan',
                                        datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                        dry_run)
                if ok:
                    inserted += 1
                    print(f"  {rd_key} ↔ ZD#{zd_id} (ticket absent de la base locale)")
                continue

            zd_id_db, org_id, tags_str, priority = row
            tags = json.loads(tags_str) if tags_str and tags_str.startswith('[') else \
                   (tags_str.split(',') if tags_str else [])
            ring_ver = version_from_tags(tags)
            sf_id, customer = resolve_sf(cur, org_id)

            ok = upsert_rd_customer(cur, rd_key, zd_id, str(org_id) if org_id else None,
                                    sf_id, customer, ring_ver, priority, 'jira_comment_scan',
                                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                    dry_run)
            if ok:
                inserted += 1

        if (i + 1) % 25 == 0:
            print(f"  Jira→ZD : {i+1} RD traités, {inserted} liens trouvés jusqu'ici")
        if not dry_run:
            cur.connection.commit()

    print(f"  Jira→ZD : {inserted} nouveaux liens, {not_in_db} tickets ZD absents de la base locale")
    return inserted

# ---------- enrichissement salesforce_id ----------

def enrich_salesforce_ids(cur, dry_run):
    """
    Pour les lignes de rd_customers sans salesforce_id mais avec organization_id
    présent dans org_to_salesforce, renseigne salesforce_id + customer.
    """
    cur.execute("""
        SELECT rc.rowid, rc.rd_key, rc.zendesk_ticket, rc.organization_id
        FROM rd_customers rc
        WHERE rc.salesforce_id IS NULL AND rc.organization_id IS NOT NULL
    """)
    rows = cur.fetchall()
    updated = 0
    for rowid, rd_key, zd_id, org_id in rows:
        sf_id, customer = resolve_sf(cur, org_id)
        if sf_id:
            if not dry_run:
                cur.execute("UPDATE rd_customers SET salesforce_id=?, customer=? WHERE rowid=?",
                            (sf_id, customer, rowid))
            updated += 1
    if not dry_run:
        cur.connection.commit()
    print(f"  Enrichissement : {updated} lignes rd_customers mises à jour avec salesforce_id")
    return updated

# ---------- rapport ----------

def write_report(cur, path):
    cur.execute("""
        SELECT rc.rd_key, ri.summary, rc.zendesk_ticket, rc.organization_id,
               rc.salesforce_id, rc.customer, rc.ring_version_tag, rc.priority,
               rc.link_source, rc.ingested_at
        FROM rd_customers rc
        LEFT JOIN rd_issues ri ON ri.issue_key = rc.rd_key
        WHERE rc.salesforce_id IS NULL
        ORDER BY rc.rd_key, rc.zendesk_ticket
    """)
    rows = cur.fetchall()
    if not rows:
        print(f"  Rapport : aucune ligne sans salesforce_id — rien à exporter")
        return
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['rd_key', 'summary', 'zendesk_ticket', 'organization_id',
                    'salesforce_id', 'customer', 'ring_version_tag', 'priority',
                    'link_source', 'ingested_at'])
        w.writerows(rows)
    print(f"  Rapport : {len(rows)} liens sans salesforce_id → {path}")

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(
        description='Corrèle tickets RD Jira ↔ tickets ZD et enrichit rd_customers')
    ap.add_argument('db', help='Chemin vers la base SQLite')
    ap.add_argument('--from-zd', action='store_true',
                    help='Scan ZD → Jira uniquement (pas de scan Jira)')
    ap.add_argument('--from-jira', action='store_true',
                    help='Scan Jira → ZD uniquement (pas de scan ZD)')
    ap.add_argument('--only-open', action='store_true',
                    help='Jira→ZD : traite seulement les RD non-Done')
    ap.add_argument('--since', metavar='DATE',
                    help='Filtre sur updated >= DATE (YYYY-MM-DD)')
    ap.add_argument('--report', metavar='FILE',
                    help='CSV des liens sans salesforce_id (à compléter via build_org_map.py)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Affiche les actions sans modifier la base')
    ap.add_argument('--sleep', type=float, default=0.3,
                    help='Pause entre appels API (secondes, défaut 0.3)')
    args = ap.parse_args()

    do_zd = args.from_zd or (not args.from_zd and not args.from_jira)
    do_jira = args.from_jira or (not args.from_zd and not args.from_jira)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    ensure_columns(cur)
    conn.commit()

    # Charge les RD connus et les ZD connus
    cur.execute("SELECT issue_key FROM rd_issues")
    known_rds = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT id FROM zendesk_tickets")
    known_zd_ids = {r[0] for r in cur.fetchall()}

    print(f"Base : {len(known_rds)} RD, {len(known_zd_ids)} tickets ZD")

    total_inserted = 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if do_zd:
        print("\n=== Scan ZD → Jira ===")
        try:
            zd = Zendesk()
        except KeyError as e:
            print(f"ERREUR : variable d'env manquante pour Zendesk : {e}")
            zd = None
        try:
            jira = Jira()
        except KeyError as e:
            print(f"ERREUR : variable d'env manquante pour Jira : {e}")
            jira = None
        if zd and jira:
            n = scan_from_zd(jira, zd, cur, known_rds, args.since, args.dry_run, args.sleep)
            total_inserted += n
            if not args.dry_run:
                conn.commit()

    if do_jira:
        print("\n=== Scan Jira → ZD ===")
        try:
            jira = Jira()
        except KeyError as e:
            print(f"ERREUR : variable d'env manquante pour Jira : {e}")
            jira = None
        if jira:
            n = scan_from_jira(jira, cur, known_zd_ids, args.since,
                               args.only_open, args.dry_run, args.sleep)
            total_inserted += n
            if not args.dry_run:
                conn.commit()

    # Enrichissement salesforce_id pour les lignes déjà présentes
    print("\n=== Enrichissement salesforce_id ===")
    enrich_salesforce_ids(cur, args.dry_run)

    # Stats finales
    cur.execute("SELECT COUNT(*) FROM rd_customers")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM rd_customers WHERE salesforce_id IS NOT NULL")
    with_sf = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT rd_key) FROM rd_customers")
    distinct_rd = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT salesforce_id) FROM rd_customers WHERE salesforce_id IS NOT NULL")
    distinct_clients = cur.fetchone()[0]

    print(f"\n=== Résultat ===")
    print(f"  Nouveaux liens insérés : {total_inserted}")
    print(f"  rd_customers total     : {total} ({with_sf} avec salesforce_id)")
    print(f"  RD distincts liés      : {distinct_rd}")
    print(f"  Clients distincts      : {distinct_clients}")

    # Top RD multi-clients (signal d'alerte)
    cur.execute("""
        SELECT rd_key, COUNT(DISTINCT salesforce_id) n_clients,
               COUNT(DISTINCT zendesk_ticket) n_tickets,
               GROUP_CONCAT(DISTINCT customer) clients
        FROM rd_customers
        WHERE salesforce_id IS NOT NULL
        GROUP BY rd_key
        HAVING COUNT(DISTINCT salesforce_id) >= 2
        ORDER BY n_clients DESC, n_tickets DESC
        LIMIT 15
    """)
    rows = cur.fetchall()
    if rows:
        print(f"\n  === RD touchant ≥ 2 clients (alerte) ===")
        for r in rows:
            ri = cur.execute("SELECT summary FROM rd_issues WHERE issue_key=?",
                             (r['rd_key'],)).fetchone()
            summary = (ri['summary'] if ri else '?')[:55]
            print(f"  {r['rd_key']:10s} {r['n_clients']} clients, {r['n_tickets']} tickets | {summary}")
            print(f"             → {r['clients']}")

    if args.report:
        write_report(cur, args.report)

    if not args.dry_run:
        cur.execute("""INSERT INTO sync_history
                       (sync_date, source, records_updated, records_added, status)
                       VALUES(?,?,0,?,'OK')""",
                    (datetime.date.today().isoformat(),
                     f"build_rd_zd_map ({'ZD+Jira' if do_zd and do_jira else 'ZD' if do_zd else 'Jira'})",
                     total_inserted))
        conn.commit()
    conn.close()

if __name__ == '__main__':
    main()
