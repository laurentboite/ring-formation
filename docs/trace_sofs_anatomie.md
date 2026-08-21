# Tracé au code de l'anatomie SOFS

Objectif : établir, comme les quatre pages d'anatomie S3 le font pour CloudServer
et sproxyd, le chemin d'appel réel des opérations SOFS — du syscall au disque —
avec les fichiers et les lignes.

## Provenance

- Générateur : `github.com/scality/ai-doc-generator`, branche `ene-testing`
- Sous-module `sources/ring` épinglé au commit `19a1567f652593a2eba69337eea927e6a248a05c`
- Récupéré en *shallow fetch* de ce commit exact (254 Mo)
- Les autres sous-modules utiles et leurs commits épinglés :
  `sources/scality-nfsd` → `b67e6cd0`, `sources/nfs-ganesha` → `b5bc18e7`

En cas de désaccord entre le code et ce document, le code l'emporte.

## La chaîne, en trois étages

```
modules/sfused/src/src/fuse/fsops.c          ← point d'entrée FUSE
   table des opérations, lignes 2547–2578
        ↓
modules/nasdk/src/libscal/fs/                ← API filesystem + retry
   timeout.c, filehdl.c, api.c, ringfs.c
        ↓
modules/nasdk/src/libscal/{sparse,db,cache,dlm,arc,key,quota,scrubber}
```

### Un détail d'architecture que le cours ne dit pas : deux drivers en cascade

`scal_fs_mkdir` (`fs/src/api.c:696`) n'appelle pas directement l'implémentation.
Elle traverse une **cascade de drivers** : d'abord `rootfs`, et seulement si
celui-ci répond `SCAL_FS_EFORWARD`, le driver `ringfs` (`api.c:713-728`).

Les deux tables sont enregistrées explicitement :
- `ctx->driver.mkdir = rootfs_mkdir` — `fs/src/rootfs.c:4873`
- `ctx->driver.unlink = rootfs_unlink` — `fs/src/rootfs.c:4874`
- `ctx->driver.unlink = ringfs_unlink` — `fs/src/ringfs.c:11989`

Le type du driver est déclaré en `fs/include/fs-structs.h:712` (typedef) et
`:763` (champ). C'est le pendant, dans le code, de la page de documentation
« RING Driver Types » que la page actuelle cite déjà deux fois.

## mkdir

| Étage | Emplacement |
|---|---|
| Entrée FUSE | `fsops.c:994` — `sfuse_mkdir` |
| Retry | `fs/src/timeout.c:552` — `scal_fs_mkdir_timeout` |
| Cascade de drivers | `fs/src/api.c:696` — `scal_fs_mkdir` |
| Implémentation | `fs/src/ringfs.c:5631` — `ringfs_mkdir` |

**Le retry a une liste d'erreurs qu'il ne réessaie jamais** (`timeout.c:571-579`) :
`SUCCESS`, `EEXIST`, `EACCES`, `EROFS`, `EPERM`, `ENAMETOOLONG`, `EDQUOT`,
`EINVAL`. Toute autre valeur repart dans la boucle. C'est ce qui distingue une
erreur métier d'une erreur transitoire.

Dans `ringfs_mkdir`, l'ordre des opérations observé :

1. ouverture du répertoire parent en écriture — `ringfs_ll_dirdb_open_rw`
2. `lookup_entry` sur le nom, pour détecter l'existant
3. consommation de quota par inode — `scal_quota_tconsume_ino`, avec un objet
   de transaction `tscal_quota_transac`
4. création de l'entrée — `ll_dirdb_creat`
5. invalidation du cache d'entrées de répertoire sur **le parent et le nouvel
   inode** — `ringfs_dcache_invalidate_ino`

Le quota n'est donc pas vérifié en amont puis appliqué : il est consommé dans la
transaction, ce qui explique le `EDQUOT` de la liste des erreurs non réessayées.

## write

| Étage | Emplacement |
|---|---|
| Entrée FUSE | `fsops.c:1485` — `sfuse_write` |
| API | `fs/src/filehdl.c:265` — `scal_fs_pwrite` |
| Driver fichier | `fs/src/ringfs_sparse.c:1591` — enregistre `ringfs_sparse_pwrite` |
| Couche sparse | `sparse/src/sparse.c:6112` — `scal_sparse_pwrite` |

`scal_fs_pwrite` n'est pas un simple passe-plat. Avant d'écrire, elle évalue si
un `stat` est nécessaire (Recycle actif, quota actif ou protection activée), et
si la Recycle est active, elle **refuse l'écriture sur un fichier marqué pour la
corbeille** en renvoyant `SCAL_FS_EPERM` (`filehdl.c:305-310` environ).

## read, et la lecture partielle

| Étage | Emplacement |
|---|---|
| Entrée FUSE | `fsops.c:1434` — `sfuse_read` |
| API | `fs/src/filehdl.c:155` — `scal_fs_pread` (passe-plat vers `driver->pread`) |
| Driver fichier | `fs/src/ringfs_sparse.c:839` — `ringfs_sparse_pread` |
| Couche sparse | `scal_sparse_read` |

**Le calcul qui justifie « lire une plage ne réveille que les stripes utiles »**
est explicite, `sparse/src/sparse.c:5152-5166` :

```c
stripe_size  = obj->write_layer->stripe_size;
end          = offset + length;
start_stripe = offset / stripe_size;
end_stripe   = (end - 1) / stripe_size;
nstripes     = end_stripe - start_stripe + 1;
...
if (stripeno < start_stripe || stripeno > end_stripe)
        continue;                       /* stripe hors plage : ignorée */
```

Puis trois cas traités séparément — première stripe, stripes intermédiaires,
dernière — et un chemin rapide quand `nstripes == 1`.

Côté écriture, la même arithmétique sert d'invariant de cohérence : un morceau
doit tenir **dans une seule stripe**, sinon l'objet est déclaré corrompu —
`SCAL_SPARSE_ECORRUPT`, `sparse.c:2471-2476`.

## unlink et la suppression

| Étage | Emplacement |
|---|---|
| Entrée FUSE | `fsops.c:1046` — `sfuse_unlink` |
| Retry | `fs/src/timeout.c:747` — `scal_fs_unlink_timeout` |
| Implémentation | `fs/src/ringfs.c:7550` — `ringfs_unlink` |
| Couche sparse | `sparse/src/sparse.c:4296` — `scal_sparse_delete` |

`ringfs_unlink` consulte le contexte Recycle (`ctx->fs->recycle_ctx`) et lit la
marque de corbeille du parent — `recycle_get_recycle_mark_value`. La corbeille
n'est donc pas un traitement en aval : elle intervient dans le chemin du
`unlink`.

### `fast_delete` : le code dit plus que la documentation

Le cours affirme aujourd'hui : « un fichier sans stripe se supprime en
supprimant son main chunk ». C'est exact, et le code précise ce qui se passe
quand la condition n'est pas remplie.

`do_fast_delete` (`sparse.c:4257`) commence par un `scal_sparse_stat`, puis
teste `scal_sparse_layout_has_data`. Si l'objet a des données :

```c
if (has_data)
        /* cannot delete the fast way */
        return SCAL_SPARSE_EINVAL;
```

Et l'appelant retombe sur le chemin normal (`sparse.c:4302-4308`) :

```c
if (ctx->fast_delete) {
        ret = do_fast_delete(ctx, oid, cos);
        if (ret != SCAL_SPARSE_EINVAL)
                goto out;
        /* EINVAL: fall back to regular delete flow */
}
```

**Conséquence opérationnelle :** activer `fast_delete` n'est pas un pari. Le
mécanisme se dégrade proprement — il tente la voie rapide, et repasse
silencieusement par la voie normale dès que le fichier a des données. La voie
rapide, elle, se réduit à un `scal_cache_delete` sur la clé principale.

## Les valeurs par défaut, dans le code

Toutes dans `sparse/include/sparse.h`, lignes 29-66 :

| Constante | Valeur | Correspondance dans le cours |
|---|---|---|
| `SCAL_SPARSE_DEFAULT_MAIN_COS` | `4` | `main_cos` 4 |
| `SCAL_SPARSE_DEFAULT_PAGE_COS` | `2` | `page_cos` 2 |
| `SCAL_SPARSE_DEFAULT_STRIPE_COS` | `2` | `stripe_cos` 2 |
| `SCAL_SPARSE_DEFAULT_STRIPE_SIZE` | `(1024 * 128)` — chaîne `"128Ki"` | `stripe_size` 131 072 |
| `SCAL_SPARSE_DEFAULT_ALLOW_STRIPE_SPLIT` | `0` | `allow_stripe_split` 0 |
| `SCAL_SPARSE_DEFAULT_FAST_DELETE` | `0` | `fast_delete` |

`stripe_cos` est en réalité dans `sparse/include/sparse_layout.h`.

Valeurs voisines que le cours ne mentionne pas et qui méritent d'y entrer :
`DIRTY_LIMIT` 1 Gio, `PARTIAL_COMMIT` 512 Mio (la moitié du précédent),
`WORKERS_IO` et `WORKERS_COMMIT` 64, `DIRTY_TIMEOUT` 5 s, `CACHE_TIMEOUT` 10 s,
`PARALLEL_STRIPE_DELETES` 1024, `UWRITE_THRESHOLD` 100 Mio.

## Ce qui reste à établir

- L'index de stripes a un **format binaire versionné**
  (`tscal_sparse_stripe_index_version`, sérialisation `sparse.c:248-331`). C'est
  vraisemblablement ce que le cours appelle « pages b-tree », mais je ne l'ai pas
  vérifié.
- L'emplacement exact de MESA reste à confirmer : le mot apparaît dans
  `sparse/`, `db/` et `fs/src/dirdb_multi.c`, sans qu'un module porte ce nom.
- Le chemin entre la couche sparse et biziod (écriture réelle sur disque) n'a pas
  été suivi.
- `replication_size_threshold` n'a été trouvé que dans `arc/src/arc_utest.c`,
  donc dans un test. Sa définition de production est ailleurs.

---

# Les quatre points ouverts, résolus

## 1. MESA, c'est la couche `db/`

Aucun module ne porte ce nom, et c'est pour cela que la recherche échouait.
MESA est la base clé-valeur implémentée dans `modules/nasdk/src/libscal/db/`,
exposée au filesystem par le backend `dirdb`. Les preuves sont dans les
commentaires du code :

- `fs/src/dirdb_multi.c:6` — `@brief  mesa backend`
- `sparse/src/sparse.c:698` — « Set the default class of service (COS) for the
  **MESA B-tree chunks** » : c'est la documentation de `page_cos`
- `sparse/src/sparse.c:719` — « number of extra parts to require for MESA
  B-tree chunks »
- `db/include/io_chunk-structs.h:125` — « number of chunk partitions, excluding
  **MESA pages** »
- `quota/src/quota.c:75` — « page keys that back the **MESA index** »
- `fs/src/rootfs.c:1346` — « A lookup in the **mesa index** must be performed »
- `fs/src/connector_id.c:205, 255, 363, 447` — le connector id est stocké dans
  une « mesa db »

Conséquence pour le cours : les « pages b-tree » dont parle l'aide-mémoire sont
les pages du B-tree **de MESA**, et `page_cos` est littéralement leur Class of
Service. Le lien entre les deux notions était deviné ; il est maintenant établi.

## 2. L'index de stripes a un format binaire documenté dans l'en-tête

`sparse/include/sparse.h:395-423`. Deux versions, et un invariant qui explique
l'arithmétique de la lecture partielle :

> In all db stripe index versions, **all stripes have the same size**, allowing
> to make the correspondence between the stripe number and an offset in the file
> data.

- **Version 1** — clé : numéro de stripe ; valeur : la clé RING de la stripe en
  ASCII terminé par NUL, 42 octets au plus
- **Version 2 (courante)** — clé : numéro de stripe ; valeur : un blob de
  40 octets

```
      20 octets        1 octet    19 octets
+-------------------+---------+-----------------+
|  clé RING binaire |  flags  |    réservé      |
+-------------------+---------+-----------------+
```

Noter que c'est un « **db** stripe index » : l'index vit dans MESA, ce qui
referme la boucle avec le point 1.

Découverte annexe : une machine à états d'**offload** existe dans la même
structure — `SPARSE_OFFLOAD_{NONE, STARTING, IN_PROGRESS, COMMITTING,
COMMITTED, ABORTED}` (`sparse.h:426-437`). Le cours n'en dit rien, alors que ses
valeurs par défaut occupent huit constantes de `sparse.h`.

## 3. Le chemin jusqu'à biziod, et où s'arrête `nasdk`

```
libscal/sparse          scal_sparse_pwrite / _read
    ↓ scal_cache_*
libscal/cache           entrées de cache, ouverture/relâche
    ↓ scal_ring_driver_*
libscal/ring            fabrique de drivers — trois types nommés :
                        « chord », « arc », « arcdata »
                        (chord/src/ring_driver_factory.c:242-262)
    ↓
libscal/chord           chord_put.c · chord_get.c · chord_delete.c
    ↓ scal_net
libscal/net             conn.c · httpreq.c
    ⇒⇒ réseau ⇒⇒
modules/ov_daemons/src/msgstore/modules/protocol/chord/include/chordcmd-def.h
                        le protocole sur le fil — c'est déjà la référence que
                        citent les pages d'anatomie S3
modules/bizio/src/biziod/bizobj/
                        biziod, un par disque, et son index bizobj
```

`nasdk` est donc entièrement **côté client**. La frontière est le protocole
`chordcmd` : au-delà, on est dans `ov_daemons` et `bizio`, exactement là où les
pages S3 s'arrêtent aussi. Les deux familles de pages se rejoignent à cet
endroit précis.

## 4. `replication_size_threshold` est une option de configuration à chaud

Ce n'était pas une constante de test : c'est une option `nconf` déclarée en
production, `arc/src/ring_driver_arc.c:496-505`.

- valeur par défaut : `SCAL_ARC_DEFAULT_REPLICATION_SIZE_THRESHOLD` = **60000**
  (`arc/include/arc-structs.h:58`)
- mode : **`SCAL_NCONF_RDWR`** — modifiable à chaud, là où le voisin
  `min_data_part_length` est `SCAL_NCONF_RDONLY`
- la description dans le code dit exactement ce que le cours enseigne : « below
  the threshold, data is replicated; at or above the threshold, data is striped
  and written to ARC. A value of -1 means replication is always used »

Deux constantes voisines, absentes du cours :
`SCAL_ARC_DEFAULT_MIN_DATA_PART_LENGTH` = 20000,
`SCAL_ARC_DEFAULT_N_DATA_PARTS` = 14 et `N_CODING_PARTS` = 4 — soit un schéma
ARC 14+4 par défaut dans le code, quand le cours enseigne 7+5 comme référence de
plateforme trois serveurs.

## Et une preuve inattendue : « CoS n = n+1 copies », dans le code

La fonction qui calcule l'espace consommé, `arc/src/ring_driver_arc.c:647-660` :

```c
if (in < scal_nconf_handle_get_ull(arc_ctx->replication_size_threshold_hdl))
  {
    u8 cos = scal_nconf_handle_get_ull(arc_ctx->object_class_hdl);
    *out = (cos + 1) * in;
    return SCAL_RING_SUCCESS;
  }
```

En dessous du seuil, l'espace consommé vaut **`(classe + 1) × taille`**. C'est
l'arbitrage que nous avions tranché sur quatre sources documentaires contre une
affirmation de formation : il est désormais prouvé par le code, en une ligne.

---

# Le maillon manquant : bizstorenode → biziod

Tracé dans le même arbre (`sources/ring` @ `19a1567f`), côté serveur cette fois.

## Le transport est une socket Unix, une par disque

`bizstorenode` ne parle pas à un biziod global : le module `iodclient` maintient
une **liste de disques**, chaque entrée portant son identifiant, et ouvre pour
chacune une socket dont le chemin est construit ainsi
(`ov_daemons/…/protocol/iodclient/src/iodclient_disks.c:707`) :

```
/run/scality/bizio/bizio_socket_<nom>.port
```

L'agent annoncé est littéralement `"Scality RING <version> (bizstorenode :<nodeinfo>)"`
(`:732`), et l'URI de store a la forme `/store/<strategy>/<dso>/0` (`:718`).

Deux enseignements. Le « un biziod par disque » du cours se voit donc **dans le
transport** : ce n'est pas seulement un choix de déploiement, c'est une socket
par instance. Et le backend de biziod est **enfichable** — d'où le `strategy` et
le `dso` dans l'URI, et d'où le nom du fichier `bizobj_strategy.h` : `bizobj`
est *une* stratégie de stockage, pas la seule.

## Une clé porte trois chunks, pas un

L'API du backend prend trois chunks distincts par opération —
`iodclient_backend.h:88` :

```c
tchunk *data, tchunk *usermd, tchunk *md
```

Et le commentaire des CRC parle de « (meta, usermd, data) »
(`iodclient-structs.h:121`). Ce que le cours appelle « l'objet » est donc, au
niveau du disque, un triplet : les données, les métadonnées utilisateur, les
métadonnées système.

Cela recoupe exactement la structure de l'enregistrement dans `bizobj.bin`, qui
contient un tableau d'en-têtes **par type d'objet** :
`tbizobj_obj_hdr objs[BIZIO_N_OBJ_TYPES]` (`bizobj_strategy.h:97`).

## Les 512 octets par clé : confirmé, et expliqué

Le cours affirme, d'après la documentation, que `bizobj.bin` « stores one record
of 512 bytes for each object key ». Le code le confirme, et dit **pourquoi** :

```c
#define BIZOBJ_ROUNDUP(x) _BIZIO_ROUNDUP(x, 512)      /* bizobj_strategy.h:29 */

typedef struct bizobj_rec_pad
{
  tbizobj_rec_hdr  hdr;
  u_char           pad[BIZOBJ_ROUNDUP(sizeof (tbizobj_rec_hdr))
                       - sizeof (tbizobj_rec_hdr)];
} tbizobj_rec_pad;                                    /* :106-113 */
```

L'en-tête d'enregistrement est plus petit que 512 octets ; le champ `pad`
comble jusqu'au multiple de 512. Le commentaire précise que ce rembourrage
n'est pas perdu — il sert à stocker les métadonnées système : « The padding is
used to store the sysmd ».

La formule de dimensionnement du cours est donc juste, et pour une raison
précise : **chaque enregistrement occupe un multiple de 512 par construction**.

## Ce que contient un enregistrement

`tbizobj_rec_hdr` (`bizobj_strategy.h:93-104`) : le numéro d'instance,
l'identifiant sérialisé — la clé —, le tableau d'en-têtes par type d'objet, un
sélecteur, un CRC d'en-tête, et des drapeaux :

| Drapeau | Sens |
|---|---|
| `BIZOBJ_RFLAG_PSEUDO` | enregistrement **pas encore commité** |
| `BIZOBJ_RFLAG_DELETED` | enregistrement supprimé |
| `BIZOBJ_RFLAG_FREE` | fait partie de la liste libre |

`BIZOBJ_RFLAG_DELETED` est le marqueur que les pages d'anatomie S3 appellent
*tombstone*, et `BIZOBJ_RFLAG_PSEUDO` éclaire l'« objet caché » de la phase GC :
un enregistrement non commité.

## Les quatre fichiers d'un store

`bizobj_strategy.h:21-24` — le cours ne nomme que les deux premiers :

| Suffixe | Rôle |
|---|---|
| `.bin` | le catalogue principal — c'est `bizobj.bin` |
| `.dat` | les fichiers de données |
| `.log` | le journal des fichiers de données |
| `.dai` | les résumés de fichiers de données |

L'en-tête principal (`bizobj_main_hdr`) porte un `cur_off`, « current offset in
catalog (bizobj.bin) », et `n_recs`, le nombre d'enregistrements.

## Les boutons du relocator, dans le code

Le cours enseigne la Relocation comme l'étape qui rend enfin l'espace. Ses
commandes de contrôle sont énumérées (`bizobj_strategy.h`, enum `BIZOBJ_CTL_*`) :
réveil du *sweeper*, réveil du *statcheck*, nombre de relocations concurrentes,
plages horaires (`SET_TIMESLOTS`), démarrage et arrêt, ratio minimal
(`RELOC_RATIO_MIN`), gestion des anciens blobs.

Autrement dit, la Relocation n'est pas seulement une tâche de fond : c'est une
tâche **pilotable**, avec des plages horaires et un ratio de déclenchement.

## Ce qui reste, désormais, hors du tracé

Ce que fait `bizstorenode` d'une commande `chordcmd` reçue — le fichier
`bizstorenode.c` ne fait que 58 lignes et délègue à des modules dont je n'ai pas
suivi l'assemblage.
