# Système d'alerting par tendances RING — Récapitulatif des travaux

**Date :** 21 juin 2026
**Objectif :** détecter qu'une version RING (ou un OS) provoque des séries de pannes, en corrélant version × composant × OS × use case × profil client, à partir d'une base SQLite enrichie depuis Salesforce, Zendesk et Jira.
**Principe directeur :** l'**ID Salesforce** est la clé de référence unique. Tout enregistrement se rattache à un compte via `salesforce_id`. Le nom de client n'est qu'un libellé d'affichage (volatil : casse, accents, variantes), jamais une clé de jointure.
**Cible :** un pipeline de mise à jour rejouable (« 1-clic »), entièrement scriptable.

---

## 1. Architecture en deux parties

Le système distingue deux natures de données, de rythmes différents :

**Base stable** — le référentiel qui change lentement. Sert de *dénominateur* à l'alerting (quelle version tourne chez qui, sur quel OS, pour quel usage). Inclut l'install base RING, les comptes, la table de conversion, ainsi que les RD et TS déjà résolus (Done).

**Partie vivante** — le flux qui bouge en continu. Sert de *numérateur* (les symptômes qui arrivent) : tickets ZD ouverts/pending, RD et TS non résolus, incidents récents. *(À construire — voir §6.)*

L'alerte naît du **croisement des deux** : un pic dans le vivant, rapporté au parc stable, filtré par version × composant × OS × use case × profil client.

Le critère de bascule vivant → stable est le **statut** (Done/Closed = stable ; Open/Pending/In progress = vivant), la date servant de filtre de fenêtre.

**Fréquences de rafraîchissement retenues :**

| Donnée | Fréquence | Justification |
|---|---|---|
| Vivant (ZD ouverts, RD/TS non-Done) | Quotidien | La précocité fait la valeur de l'alerte |
| Stable (install base, comptes, RD/TS Done) | Hebdomadaire | Le parc évolue à l'échelle de la semaine |
| `org_to_salesforce` | Mensuel + à la demande | org_id stables, seuls les nouveaux clients comptent |

Le calcul d'alerte tourne à la cadence du vivant (quotidien) et lit le socle stable tel quel.

---

## 2. État de la base enrichie

Fichier livré : `ring_install_base_updated_20260621.db`

| Table | Lignes | Rôle |
|---|---|---|
| `installations` | 475 | Install base RING (enrichie : SP, industry, use cases, OS) |
| `use_case_normalized` | 740 | Use cases RING multi-label reconstruits |
| `customer_use_cases` | 333 | Use cases d'origine |
| `zendesk_tickets` | 1272 | Tickets ZD (figés au 16/05/2026) |
| `rd_issues` | 324 | RD **tous Done** — partie stable (cause racine produit) |
| `rd_customers` | 101 | **Liaison RD → client** (nouveau) |
| `org_to_salesforce` | 35 | **Table de conversion org_id ZD → salesforce_id** (nouveau) |
| `field_observations` | 5600 | Traçabilité multi-source des attributs |
| `jira_issues` | 100 | TS d'origine |
| `os_update_audit` | 480 | Audit réversible des écrasements OS |

Tables de documentation associées : `rd_issues_doc`, `rd_customers_doc`, `field_observations_doc`.

---

## 3. Le pont Zendesk → Salesforce (verrou principal résolu)

**Problème :** aucun pont automatique préexistant entre l'organisation Zendesk d'un ticket et le compte Salesforce.
- Côté tickets ZD : `external_id` systématiquement vide, aucun custom_field ne porte d'ID Salesforce.
- Côté Salesforce : sur les 72 objets et tout le schéma du compte, aucun champ « Zendesk Org ID », aucun objet de liaison.

**Solution retenue : une table de conversion `org_to_salesforce`** (organization_id → salesforce_id), persistante, construite une fois puis relue à chaque run. L'org_id Zendesk étant stable dans le temps, la correspondance reste valable.

**Déblocage clé :** la recherche `type:organization <nom>` dans Zendesk retourne les organisations avec `id`, `name`, `domain_names` et parfois `external_id`. Le rapprochement se fait donc par **nom de compte** (sens Salesforce → Zendesk), avec validation des cas ambigus.

**Points d'attention identifiés :**
- Un client a souvent **plusieurs org_id** (ex. Uniserver : org principale + org legacy portant les tickets RD ; KPN + KPN Security). La table capte tous les org_id d'un même compte.
- Certaines organisations anciennes portent un `external_id` court (EDF=305, SocGen=457, Comcast=16, GCPD=333, Airbus DS Geo=441, Amadeus=414, CLS=458) — piste d'un second pont à tester côté Salesforce.

**État actuel :** 35 organisations mappées, toutes avec les deux ID (Zendesk + Salesforce). Couvre les 30 comptes liés aux RD, dont les multi-org Uniserver et KPN.

---

## 4. Lien RD → client (`rd_customers`)

Matérialise la chaîne de causalité **RD (cause racine produit) → ticket ZD (symptôme client) → organisation → salesforce_id (client)**.

Construction : extraction des références « RD-… » dans le sujet, la description et les champs des 410 tickets ZD « RD- », puis rattachement via `org_to_salesforce`.

**Résultats :**
- 101 liaisons (RD, ticket), dont **44 rattachées à un compte**.
- 40 RD distincts rattachés, chez 15 clients.
- Chaque liaison porte les dimensions d'alerting : version RING, environnement, criticité (p1–p4), cause (`cause_product_bug`, etc.).

**Signal de tendance déjà visible :** RD-830 touche 3 clients, RD-125 en touche 2 (EDF + NIC) — exactement le type de motif « un bug produit lié à une version frappe plusieurs clients » que le système doit détecter.

Les 57 liaisons « org non mappée » se rattacheront automatiquement une fois la table de conversion étendue à toute la base.

---

## 5. Constats de qualité sur les sources

**RD (projet R&D, Jira) :** mélange bugs clients et tests CI (issuetype=Test exclus). Pas d'ID Salesforce dans les RD. Le rattachement client fiable se fait via le tag `[Client]` en tête de titre, pas par mots du nom (faux positifs massifs sinon). « Done » ≠ bug toujours confirmé (certains fermés « disregard »).

**TS (projet Technical Services, Jira) :** la base figée montre des TS d'upgrade en Ongoing/Backlog alors que Jira live les montre Done avec date de résolution réelle. **Le statut figé ment ; lire Jira live est la bonne source d'upgrade.** Salesforce sous-estime systématiquement la date d'upgrade — la `resolutiondate` du TS est la référence.

**Lien RD ↔ ZD :** peu matérialisé et non normalisé. Format libre dans les champs ZD. La recherche ZD « RD- » (410 tickets sur 12 mois) est le bon sous-ensemble pour reconstruire le lien.

---

## 6. Reste à faire

- **Partie vivante :** récupérer les RD non-Done (Open/In Progress) — le signal d'alerting le plus précoce — ainsi que les tickets ZD ouverts et TS en cours.
- **Étendre `org_to_salesforce`** aux ~302 comptes RING restants via le script autonome (voir §7), puis régénérer `rd_customers`.
- **Tester les `external_id` legacy** (305, 457…) comme second pont automatique côté Salesforce.
- **Rafraîchir l'archive ZD** (la table `zendesk_tickets` est figée au 16/05/2026).
- **Construire l'orchestrateur** du pipeline selon les trois cadences (vivant / stable / conversion).
- Compléter les `salesforce_id` manquants des tickets ZD via la table de conversion.
- Vérifier manuellement ~7 écrasements OS douteux.

---

## 7. Livrables

| Fichier | Contenu |
|---|---|
| `ring_install_base_updated_20260621.db` | Base enrichie (toutes les tables ci-dessus) |
| `build_org_map.py` | Script autonome de résolution org_id → salesforce_id pour toute la base, rejouable, gère les cas ambigus et le rate limit Zendesk (à exécuter dans l'environnement utilisateur avec accès Zendesk) |
| `comptes_a_resoudre_zendesk.csv` | Les 302 comptes RING restant à mapper |
| `derniere-maj-os-ring-par-client.csv` | Dernière montée de version par client (source TS Jira Done) |
| `discrepancies-upgrade-sfdc-vs-ts.csv` | Écarts de date d'upgrade SFDC vs TS (134 clients) |

**Pour étendre la table de conversion :**
```
export ZENDESK_SUBDOMAIN=scality-support ZENDESK_EMAIL=… ZENDESK_API_TOKEN=…
python3 build_org_map.py ring_install_base_updated_20260621.db --report a_valider.csv
```
Le script complète la table, préserve les lignes validées manuellement (`verified=1`) et sort la liste des cas ambigus à trancher.
