#!/usr/bin/env python3
"""
build_org_map.py — Construit/complète la table de conversion org_to_salesforce
(organization_id Zendesk  <->  salesforce_id) pour TOUS les comptes RING.

Conçu pour le pipeline 1-clic : rejouable, idempotent, déterministe.
La clé de référence est le salesforce_id ; l'organization_id Zendesk est la clé
d'entrée des tickets. Le NOM ne sert qu'au rapprochement initial et à l'affichage
(il est volatil : casse, accents, variantes — jamais utilisé comme clé de jointure).

PRÉREQUIS (environnement utilisateur, PAS le bac à sable Claude) :
  - accès réseau à l'API Zendesk Scality
  - variables d'env : ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN
  - la base SQLite ring_install_base (chemin en argument)

USAGE :
  python3 build_org_map.py /chemin/ring_install_base.db
  python3 build_org_map.py db.sqlite --only-missing      # ne traite que les comptes non mappés (défaut)
  python3 build_org_map.py db.sqlite --all               # retraite tous les comptes
  python3 build_org_map.py db.sqlite --report ambiguous.csv  # exporte les cas à valider

Le script N'ÉCRASE JAMAIS une ligne verified=1 (validation humaine préservée).
"""
import os, re, sys, csv, json, time, sqlite3, datetime, argparse
import urllib.parse, urllib.request

# ---------- normalisation des noms (uniquement pour le matching) ----------
import unicodedata
def norm(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = s.lower()
    s = re.sub(r'[^a-z0-9]', '', s)   # d'abord : ne garder que [a-z0-9]
    # puis retirer les suffixes juridiques courants collés en fin de chaîne
    for suf in ('gmbh','sro','as','too','oao','ooo','zao','pte','sdnbhd','sdn','bhd',
                'sa','ag','inc','ltd','llc','corp','srl','spa','plc','bv','nv','kg',
                'sas','sasu','cjsc','berhad','limited','pvt','coop','kgaa','co'):
        if s.endswith(suf) and len(s) > len(suf) + 2:
            s = s[:-len(suf)]
    return s

# ---------- client Zendesk ----------
class Zendesk:
    def __init__(self):
        self.sub = os.environ['ZENDESK_SUBDOMAIN']
        self.email = os.environ['ZENDESK_EMAIL']
        self.token = os.environ['ZENDESK_API_TOKEN']
        import base64
        cred = base64.b64encode(f"{self.email}/token:{self.token}".encode()).decode()
        self.auth = "Basic " + cred

    def search_org(self, name, per_page=10):
        """Recherche les organisations dont le nom matche `name`.
        Retourne (orgs, tickets) — tickets sert à capter les org_id secondaires."""
        q = f'type:organization {name}'
        url = f"https://{self.sub}.zendesk.com/api/v2/search.json?" + \
              urllib.parse.urlencode({'query': q, 'per_page': per_page})
        req = urllib.request.Request(url, headers={'Authorization': self.auth})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:  # rate limit
                    wait = int(e.headers.get('Retry-After', 60)); time.sleep(wait); continue
                raise
        else:
            return [], []
        orgs = [x for x in data.get('results', []) if x.get('result_type') == 'organization']
        tickets = [x for x in data.get('results', []) if x.get('result_type') == 'ticket']
        return orgs, tickets

# ---------- logique de matching ----------
def choose(account_name, orgs, tickets):
    """Retourne une liste de tuples (org_id, zd_name, domains, external_id, confidence).
    Plusieurs lignes possibles (client multi-org). 'ambiguous' = à valider à la main."""
    target = norm(account_name)
    exact = [o for o in orgs if norm(o['name']) == target]
    # org_id secondaires vus dans les tickets (même client, autre org)
    ticket_orgs = {}
    for t in tickets:
        oid = t.get('organization_id')
        if oid: ticket_orgs[str(oid)] = ticket_orgs.get(str(oid), 0) + 1

    def is_real(o):
        # une org "réelle" a au moins un signe : domaine, tickets partagés, AE/SE renseigné
        of = o.get('organization_fields') or {}
        return bool(o.get('domain_names')) or o.get('shared_tickets') or \
               of.get('account_executive') or of.get('sales_engineer') or o.get('external_id')

    rows = []
    if len(exact) == 1:
        o = exact[0]
        # un match exact sur un nom court/générique sans aucun signe de réalité est suspect
        conf = 'high' if (is_real(o) or len(target) >= 8) else 'ambiguous'
        rows.append((str(o['id']), o['name'], o.get('domain_names', []), o.get('external_id'), conf))
        for oid, n in ticket_orgs.items():
            if oid != str(o['id']) and n >= 2:
                rows.append((oid, account_name + ' (secondary)', [], None, 'medium'))
    elif len(exact) > 1:
        # plusieurs noms exacts : préférer celles "réelles", tout en ambiguous pour validation
        for o in exact:
            rows.append((str(o['id']), o['name'], o.get('domain_names', []), o.get('external_id'), 'ambiguous'))
    elif len(orgs) == 1:
        o = orgs[0]
        # match partiel : nom cible contenu dans le nom ZD ou vice-versa
        no = norm(o['name'])
        conf = 'medium' if (target in no or no in target) and len(target) >= 4 else 'low'
        rows.append((str(o['id']), o['name'], o.get('domain_names', []), o.get('external_id'), conf))
    else:
        # plusieurs candidats sans exact -> tout en ambiguous
        for o in orgs[:10]:
            rows.append((str(o['id']), o['name'], o.get('domain_names', []), o.get('external_id'), 'ambiguous'))
    return rows

# ---------- programme principal ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    ap.add_argument('--all', action='store_true', help='retraite tous les comptes (défaut: seulement manquants)')
    ap.add_argument('--report', help='CSV des cas ambigus à valider')
    ap.add_argument('--sleep', type=float, default=0.3, help='pause entre appels (rate limit)')
    args = ap.parse_args()

    con = sqlite3.connect(args.db); con.row_factory = sqlite3.Row; cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS org_to_salesforce(
        organization_id TEXT PRIMARY KEY, salesforce_id TEXT, customer TEXT,
        source TEXT, confidence TEXT, verified INTEGER DEFAULT 0, updated_at TIMESTAMP)""")
    con.commit()

    # comptes à traiter : par salesforce_id (clé de réf), nom pour la recherche
    cur.execute("""SELECT DISTINCT salesforce_id, customer FROM installations
                   WHERE salesforce_id IS NOT NULL AND customer IS NOT NULL AND customer!=''""")
    accounts = {r['salesforce_id']: r['customer'] for r in cur.fetchall()}

    if not args.all:
        done = set(r['salesforce_id'] for r in
                   cur.execute("SELECT DISTINCT salesforce_id FROM org_to_salesforce WHERE salesforce_id IS NOT NULL"))
        accounts = {sf: n for sf, n in accounts.items() if sf not in done}

    print(f"{len(accounts)} comptes à résoudre")
    zd = Zendesk()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ambiguous = []
    stats = {'high': 0, 'medium': 0, 'low': 0, 'ambiguous': 0, 'none': 0}

    for i, (sf, name) in enumerate(sorted(accounts.items(), key=lambda x: x[1].lower()), 1):
        try:
            orgs, tickets = zd.search_org(name)
        except Exception as e:
            print(f"  [{i}] ERREUR {name}: {e}"); continue
        rows = choose(name, orgs, tickets)
        if not rows:
            stats['none'] += 1; print(f"  [{i}] {name}: aucune org trouvée"); time.sleep(args.sleep); continue
        for org_id, zd_name, domains, ext, conf in rows:
            stats[conf] = stats.get(conf, 0) + 1
            src = 'zd_name_search' + (' +external_id' if ext else '')
            # ne pas écraser une ligne validée manuellement
            ex = cur.execute("SELECT verified FROM org_to_salesforce WHERE organization_id=?", (org_id,)).fetchone()
            if ex and ex['verified'] == 1:
                continue
            # pour les ambigus : on stocke salesforce_id=NULL (à trancher) + on logge
            sf_val = sf if conf in ('high', 'medium') else None
            cur.execute("""INSERT INTO org_to_salesforce(organization_id,salesforce_id,customer,source,confidence,verified,updated_at)
                           VALUES(?,?,?,?,?,0,?)
                           ON CONFLICT(organization_id) DO UPDATE SET
                             salesforce_id=excluded.salesforce_id, customer=excluded.customer,
                             source=excluded.source, confidence=excluded.confidence, updated_at=excluded.updated_at
                           WHERE org_to_salesforce.verified=0""",
                        (org_id, sf_val, name, src, conf, now))
            if conf == 'ambiguous':
                ambiguous.append({'salesforce_id': sf, 'account': name, 'org_id': org_id,
                                  'zd_name': zd_name, 'domains': '|'.join(domains or []), 'external_id': ext or ''})
        con.commit()
        if i % 25 == 0: print(f"  ... {i}/{len(accounts)}")
        time.sleep(args.sleep)

    print("\nRésumé:", stats)
    n = cur.execute("SELECT COUNT(*) FROM org_to_salesforce WHERE salesforce_id IS NOT NULL").fetchone()[0]
    print(f"org_to_salesforce : {n} lignes avec salesforce_id")

    if args.report and ambiguous:
        with open(args.report, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['salesforce_id','account','org_id','zd_name','domains','external_id'])
            w.writeheader(); w.writerows(ambiguous)
        print(f"{len(ambiguous)} cas ambigus -> {args.report} (à valider, puis passer verified=1)")
    con.close()

if __name__ == '__main__':
    main()
