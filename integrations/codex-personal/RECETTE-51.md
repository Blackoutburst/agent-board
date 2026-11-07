# Recette réelle #51 — plusieurs emplacements de file standing (2 puis 4)

Brouillon opérateur pour contre-revue, rédigé le 6 novembre 2026. **Rien
n'a été exécuté** : aucune préparation de paquet, aucun sudo, aucun démarrage.
Chaque commande `sudo` est destinée à un humain, après revue du Lead.
Les valeurs `<<À_REMPLIR:…>>` sont fournies par un humain ; elles ne sont
jamais déduites d'un message Board, d'un ticket ou d'un modèle.

## 1. Objet et accords

> **Statut : brouillon pour revue, non exécutable.** Contre-revue du Lead
> (`6990b166`) : pas prêt pour approbation d'exécution. Les valeurs
> `<<À_REMPLIR>>` (paquet, tickets, profils, documents) doivent être fixées
> et revues avant toute commande privilégiée bornée. Rien dans ce document
> n'autorise un sudo, une modification de HOME, une activation ou un grant.

Premier passage réel du planificateur `linux-cloud-queue-slots-v1`
(`development_queue_slots.py`, paquet 0.50.0) avec deux, puis quatre modèles
Codex simultanés, sur l'abonnement Codex de TheScreamer.

- Plan : message Board `5fc01ed0`.
- Corrections du Lead (à respecter) : message `537c7f14`.
- Approbation de TheScreamer : Needs-you `a70d7a65`.
- Approbation de Blackout : relayée par son compagnon dans `92a7c455`.
  **Il doit la confirmer directement** avant toute étape privilégiée.

Contraintes reprises du Lead :

- Le `CODEX_HOME` installé reste tel quel. Aucun fichier d'authentification
  n'est lu ni copié. Toute autre disposition est une opération humaine
  distincte, hors de cette recette.
- Les plafonds CPU/RAM proposés (CPUQuota 600 %, MemoryHigh 8G, MemoryMax
  10G, TasksMax 1024) restent une hypothèse jusqu'à leur mesure en passe 2.
  Les plafonds actuels sont CPU 300 %, MemoryHigh 4 GiB, MemoryMax 6 GiB et
  TasksMax 256. Voir la section 6 : **le code actuel ne permet pas de les
  modifier sur l'installation existante**.
- La preuve, ce sont les intervalles horaires de quatre appels modèle
  effectifs qui se chevauchent, sur des tickets et des racines distincts, avec
  versions et plafonds exacts, puis l'arrêt et le nettoyage. Quatre profils en
  ligne ou quatre PR ne prouvent rien. La preuve s'appuie sur la télémétrie
  des tours codeur de 8.2 (tours Codex, pas appels HTTP).
- Les états actifs (`admitting`, `active`, en pause, `needs_you`) restent
  comptés dans le plafond. C'est le comportement du code :
  `_Scheduler.holders` compte tout pointeur `tick.json` non idle.

## 2. État actuel relevé (6 novembre 2026, sans sudo)

| Élément | Observation | Commande |
| --- | --- | --- |
| Release active | `66cce93319dbd72e392d3b5bb2495910dfc9a284762a9a6e0e87a5561a5f23be` : adapter 0.49.1, révision `122af32c501f3b2817a096e3a7574bed082493a7`, codex 0.163.0, gh 2.94.0, `/usr/bin/python3.10` sha `a2f33a6e…c56ae`, propriétaire Board `thescreamer`, endpoint `https://board.blackoutburst.com/mcp` | `python3 -c 'import json;print(json.load(open("/opt/blackoutburst-agent-pool/releases/66cce933…/manifest.json"))["adapter_version"])'` |
| `unit_limits` du manifeste | `cpu_percent` 300, `memory_high_bytes` 4294967296, `memory_bytes` 6442450944, `tasks` 256, `lifetime_seconds` 3600, `stop_seconds` 45 | idem |
| Releases présentes | 25 répertoires sous `/opt/blackoutburst-agent-pool/releases/`. La plus récente est `66cce933…` (4 nov. 14:56), précédée de `4d3c80c6…` (0.49.0) | `ls -la /opt/blackoutburst-agent-pool/releases/` |
| Reçus root | `66cce933….complete.json` et `66cce933….continuation.complete.json` présents (mode 0600 root, contenu non lu) | `ls -la /var/lib/blackoutburst-agent-pool/installations/` |
| Unités | `blackoutburst-agent-pool-bb-agent-thescreamer.service`, `blackoutburst-agent-continuation-bb-agent-thescreamer.service` et `.timer` : loaded, inactive/dead, **disabled**, sans drop-in, ExecStart sur la release `66cce933…`. Les deux unités Host sont également disabled | `systemctl list-unit-files 'blackoutburst-agent*'` ; `systemctl show -p LoadState,ActiveState,SubState,UnitFileState,DropInPaths,FragmentPath <unité>` |
| Plafonds effectifs | `CPUQuotaPerSecUSec=3s`, `MemoryHigh=4294967296`, `MemoryMax=6442450944`, `TasksMax=256`, `RuntimeMaxUSec=1h` | `systemctl show -p … <service>` |
| Unité de continuation | `--continuation /var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/config/continuation.json` (chemin fixe dans le modèle) | `systemctl cat blackoutburst-agent-continuation-bb-agent-thescreamer.service` |
| Dernière passe | 4 nov. 15:45:13, terminée avec succès. Aucun timer actif | `journalctl -u blackoutburst-agent-continuation-bb-agent-thescreamer.service -n 6` ; `systemctl list-timers --all` |
| Hôte | 8 CPU, 15 GiB de RAM (environ 12 disponibles), 3 GiB de swap dont 2 utilisés, systemd 249, noyau 5.15.0-198, cgroup v2 | `nproc` ; `free -g` ; `systemctl --version` |
| `codex` du PATH ubuntu | `codex-cli 0.166.0`. **Sans objet** : l'unité utilise `release/tools/codex` 0.163.0 | `codex --version` |

À relever par un humain (permission refusée sans sudo) :

```sh
sudo ls -la /var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/ \
  /var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/config \
  /var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/admissions
# Schéma (et seulement le schéma) du document de continuation actuel :
sudo -u bb-agent-thescreamer -H /usr/bin/python3.10 -I -B -c 'import json;print(json.load(open("/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/config/continuation.json"))["schema"])'
# Phase de chaque pointeur de file de la racine (aucun autre champ affiché) :
sudo -u bb-agent-thescreamer -H /usr/bin/python3.10 -I -B -c 'import glob,json;[print(p,json.load(open(p)).get("phase"),json.load(open(p)).get("active") is None) for p in glob.glob("/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/admissions/queue-*/tick.json")]'
```

Bloquant : si un pointeur `RACINE/admissions/queue-*/tick.json` n'est pas
`idle` avec `active` nul, la passe slots est refusée en entier
(`continuation_pool_configuration_invalid`) avant tout claim. Il faut alors
terminer ce travail #73, et non l'effacer. Si `continuation.json` porte
encore la reprise de revue v2 (0.49.1) et que celle-ci n'est pas terminée,
le Lead décide avant de le remplacer.

**Conservation de #73.** L'ancienne récupération #73, sa configuration
(`continuation.json` actuel) et ses historiques sont conservés tels quels tant
que leur état exact n'est pas réconcilié et publié sur #51. La sauvegarde
`continuation.pre51.json` (7.2) est une copie, jamais un remplacement de ces
historiques.

`MemoryPeak` n'existe pas sous systemd 249 et `memory.peak` est absent sous
le noyau 5.15. Le pic mémoire doit donc être **échantillonné** (section 8).

## 3. Prérequis humains (aucun n'est fait par cette recette)

1. Codex : le login officiel existant du compte `bb-agent-thescreamer`
   (abonnement de TheScreamer), sous son HOME normal
   `/var/lib/blackoutburst-agent-host/owners/bb-agent-thescreamer`. On ne le
   modifie pas et on ne le copie pas. Les quatre processus Codex partagent ce
   même `.codex` (risque à observer, section 9).
2. Board : quatre profils codeurs distincts (le Board n'admet qu'un travail de
   file non terminé par profil, et le chargeur refuse deux emplacements sur le
   même `agent_id`) et un reviewer distinct, créés et appairés par des humains.
   Il faut aussi l'autorisation de file (`claim_selected_queue_ticket`) pour
   chaque codeur, les grants des ressources exactes et une capacité Runtime
   propriétaire libre d'au moins quatre exécutions codeur plus les revues. Une
   exécution incertaine ancienne reste comptée.
3. Connexions : une connexion codeur par emplacement, appairée **directement**
   dans `RACINE/slots/<nom>/connections/`. Le reviewer doit aussi avoir une
   connexion **sous la racine de chaque emplacement**, car le workflow exige
   `reviewer.connection_file` sous `<racine d'emplacement>/connections`. Cela
   fait quatre appairages du même reviewer, ou bien un reviewer par
   emplacement. Une copie de fichier de connexion est exclue. L'humain qui
   appaire vérifie que le flux d'appairage ne réécrit pas `~/.codex`
   (`connect.py` vise `~/.codex/config.toml` par défaut).
4. Projet bac à sable avec quatre tickets Todo. Chaque ticket est assigné
   explicitement au codeur de son emplacement et porte une ressource distincte
   (par exemple `repo:t51-a` … `repo:t51-d`). Sinon, l'exclusion de ressource
   sérialise les emplacements.
5. **Dépôt : écart avec le plan.** Il n'existe pas de « dépôt de test »
   possible. La destination de publication est figée dans le code
   (`git_publication.py` : `Blackoutburst/agent-board`, base `main`). Les
   quatre PR brouillon seront donc ouvertes sur `Blackoutburst/agent-board`.
   Chaque emplacement a son propre clone local de ce dépôt sous
   `repositories/`, à un commit de base approuvé, avec des chemins existants et
   disjoints par ticket. Un vrai dépôt de test demanderait un changement de code
   et une nouvelle revue. Les propriétaires doivent accepter cet écart.
   **Cette décision doit être enregistrée explicitement** (Needs-you ou
   message Board de TheScreamer, référencé ici) avant toute préparation de
   cette variante ; une approbation générale du plan ne vaut pas décision
   sur le dépôt réel.
6. gh : la connexion gh officielle existante du même compte, avec droit
   d'écriture sur ce dépôt, sans jeton copié.
7. Racines d'emplacement : créées par l'humain sous l'UID du propriétaire, en
   mode 0700. La racine est **dérivée** (`RACINE/slots/<nom>`) et n'est jamais
   lue depuis un document :

```sh
R=/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer
for s in dev-1 dev-2 dev-3 dev-4; do
  for d in config connections repositories checkouts verifiers batches git-state; do
    sudo -u bb-agent-thescreamer -H install -d -m 0700 "$R/slots/$s/$d"
  done
done
# Ne pas créer RACINE/slots/<nom>/admissions, ni RACINE/continuations : le code les crée.
```

## 4. Paquet 0.50.0

- Source : branche `claude/t51-parallel-slots`, empilée sur la PR #98
  (`2d067f33f7f471adcc9c3a78027f1dd7d0526c49`). Tête de code empaquetée :
  `3e27fcdbfdb2d702647204c4ad9568f82c8e1e95` (emplacements, télémétrie des
  tours codeur, script de prévol §7.6 corrigé pour `slots/inventory.json`). Un commit qui ne touche que la
  documentation ne modifie aucun fichier empaqueté, mais il change
  `revision`, donc l'identifiant du bundle. **Préparer depuis la tête de code
  revue et noter son SHA.**
- `pyproject.toml` : `version = "0.50.0"`.
- Modèles d'unité identiques octet pour octet à la release `66cce933…`.
  Valeurs sha256 : `unit.service.in` `4aca3364…2ebe`, `continuation.service.in`
  `054e80e3…2d35`, `continuation.timer.in` `bf66f944…2ff`. La continuité exigée
  par `upgrade` et `upgrade-continuation` est donc respectée.
- Scripts publics attendus dans le bundle (sha256 de la source) :
  `pool_manage.py` `eaebab321bcd760c4f07a034bc09ad6e702aa8c037b818ed3f5996c98f2ed859`
  (inchangé depuis 0.49.1),
  `pool_package.py` `f929527ce8f1910ddf50e9206bfa79be9b04813b6c2c77b4da2410ca2e43b9eb`,
  `pool_entry.py` `5485166f0c8c5d7b272458cb14ff4883b347a419898d03a2afafa1e4eacf13f3`.

Préparation hors ligne, non root, sous l'utilisateur ubuntu :

```sh
HEAD_SHA=3e27fcdbfdb2d702647204c4ad9568f82c8e1e95   # tête de code ; voir « Valeurs produites » ci-dessous
CAND=/home/ubuntu/agent-pool-candidates/t51-slots-${HEAD_SHA:0:8}-2026110X
install -d -m 0700 "$CAND"
git clone --no-hardlinks /home/ubuntu/src/ab-wt51 "$CAND/source"
git -C "$CAND/source" checkout --detach "$HEAD_SHA"
git -C "$CAND/source" status --porcelain   # doit être vide
```

`$CAND/public-spec.json` (0600). Il est identique au spec 0.49.1, avec les
mêmes outils copiés depuis la release active, ce qui garde les octets
`tools/*` égaux comme `upgrade` l'exige :

```json
{"board_endpoint":"https://board.blackoutburst.com/mcp","board_owner":"thescreamer",
 "bwrap":"/opt/blackoutburst-agent-pool/releases/66cce93319dbd72e392d3b5bb2495910dfc9a284762a9a6e0e87a5561a5f23be/tools/codex-resources/bwrap",
 "codex":"/opt/blackoutburst-agent-pool/releases/66cce93319dbd72e392d3b5bb2495910dfc9a284762a9a6e0e87a5561a5f23be/tools/codex",
 "codex_version":"0.163.0",
 "gh":"/opt/blackoutburst-agent-pool/releases/66cce93319dbd72e392d3b5bb2495910dfc9a284762a9a6e0e87a5561a5f23be/tools/gh",
 "gh_version":"2.94.0",
 "python_pin":{"path":"/usr/bin/python3.10","sha256":"a2f33a6e006989270f4340528eb61f8f97366e00a5d1b602ac8672ea44fc56ae"},
 "unit_limits":{"cpu_percent":300,"lifetime_seconds":3600,"memory_bytes":6442450944,"memory_high_bytes":4294967296,"stop_seconds":45,"tasks":256},
 "unix_owner":"bb-agent-thescreamer"}
```

```sh
cd "$CAND/source" && python3 -B -m deploy.worker_pool.manage prepare --source "$CAND/source" --output "$CAND/bundle" --spec "$CAND/public-spec.json"
python3 -I -B "$CAND/bundle/pool_manage.py" plan "$CAND/bundle"
sha256sum "$CAND/bundle/manifest.json" "$CAND/bundle/pool_manage.py" "$CAND/bundle/pool_package.py" "$CAND/bundle/pool_entry.py"
```

Le `bundle_id` affiché par `plan` doit être égal au sha256 de
`bundle/manifest.json` (`manifest_id` = sha256 du manifeste canonique écrit).
Vérifier aussi que `adapter_version` vaut 0.50.0, que `revision` vaut
`$HEAD_SHA`, que `unit_limits` correspond au spec et que les trois scripts ont
les sha256 ci-dessus. Le Lead consigne `NEW_BUNDLE_ID` avant tout sudo.

**Valeurs produites (préparation à blanc, non root, 7 novembre 2026).**
Construites par l'agent serveur sous ubuntu, entièrement dans son
répertoire temporaire, depuis un clone de `ab-wt51` détaché sur
`3e27fcdbfdb2d702647204c4ad9568f82c8e1e95`, sans lire `/opt`, `/var/lib`, ni
aucune racine privée. Seule différence avec la procédure ci-dessus : les
chemins `codex`/`bwrap`/`gh` du spec pointaient vers les copies du bundle
candidat 0.49.1
`/home/ubuntu/agent-pool-candidates/queue73-review0491-122af32c-20261104/bundle/tools/…`,
dont le `manifest_id` vaut exactement `PREV` (`66cce933…f23be`) et dont les
sha256 d'outils sont vérifiés contre ce manifeste. Le reste du spec est
identique (mêmes valeurs que ci-dessus).

- tête source (`revision`) : `3e27fcdbfdb2d702647204c4ad9568f82c8e1e95` ;
- `adapter_version` : `0.50.0` ; codex `0.163.0`, gh `2.94.0`, python
  `/usr/bin/python3.10` `a2f33a6e…fc56ae` ;
- `bundle_id` affiché par `prepare` et `plan` = sha256 de
  `bundle/manifest.json` :
  `f324f316e2e2b2fcc5822d06daaf4cc3d301593cb413d5c8993fb0f914c5096a` ;
- 166 fichiers ; outils : `tools/codex`
  `399ff69c431c066e40de318fba45c2b07f45a76b19939d247ee4828fc70c4215`,
  `tools/codex-resources/bwrap`
  `77360cb751ccedc5971391444ac86a8a33c15b04d6b4a6fe45f5d25496e62c4c`,
  `tools/gh` `c2033c14259a3a3b7518a47535e57385a6d3faaba1759e9cf8c1c10dd21d3de9` ;
- scripts et modèles : `pool_manage.py` `eaebab32…ed859`,
  `pool_package.py` `f929527c…3b9eb`, `pool_entry.py` `5485166f…cf13f3`,
  `unit.service.in` `4aca3364…2ebe`, `continuation.service.in`
  `054e80e3…2d35`, `continuation.timer.in` `bf66f944…2ff` : tous égaux aux
  valeurs attendues ci-dessus ;
- `unit_limits` construits :
  `{"cpu_percent":300,"lifetime_seconds":3600,"memory_bytes":6442450944,"memory_high_bytes":4294967296,"stop_seconds":45,"tasks":256}`.

Le commit qui consigne ces valeurs ne touche que ce document et
`SOURCE-MANIFEST.json`, qui ne sont pas empaquetés, mais il change la tête.
Préparer donc **depuis `3e27fcd…`** (le `checkout --detach` ci-dessus), pas
depuis la tête de la branche, pour retrouver ce `bundle_id`.

**Remplacé :** le `bundle_id` `894824aa7b1aee9fdbd6ccf7f1309714a6edd38abeba52426a96eef8d473a1bb`
consigné auparavant venait de `92a96a8…`, dont le prévol §7.6 refusait la
disposition réelle (`slots/inventory.json`). Il est caduc : ne jamais le
présenter comme contenant le script corrigé, ni l'installer. Entre les deux
préparations, seul `revision` change dans `manifest.json` (mêmes 166
fichiers, mêmes sha256) : le script de prévol n'est pas dans le bundle,
l'humain l'installe depuis `$CAND/source` détaché sur `$HEAD_SHA` (§7.6),
d'où l'importance de cette tête.

`NEW_BUNDLE_ID=<<À_REMPLIR>>` : à consigner par le Lead après que l'opérateur
a refait `prepare` + `plan` avec le spec ci-dessus (outils lus dans la
release active `/opt/…/66cce933…`) et obtenu le même `bundle_id`
`f324f316…5096a`. Une valeur différente veut dire que les outils installés
diffèrent du bundle 0.49.1 : arrêter et comparer les sha256 d'outils.

## 5. Documents à créer (propriétaire 0600, aucun lien dur ni symbolique)

Tous les chemins sont sous `R=/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer`.
Le service lit **toujours** `R/config/continuation.json` : le document slots
doit finir à ce chemin exact.

### 5.1 Document slots, passe 2 puis passe 4

`R/config/slots-51-p2.json` :

```json
{"schema":"linux-cloud-queue-slots-v1","concurrency":2,"slots":[
 {"name":"dev-1","continuation_file":"/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/slots/dev-1/config/standing.json"},
 {"name":"dev-2","continuation_file":"/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/slots/dev-2/config/standing.json"}]}
```

`R/config/slots-51-p4.json` : même structure avec `"concurrency":4` et
`dev-1` … `dev-4`. Règles du code : `concurrency` vaut de 1 à 8 et au plus le
nombre d'emplacements ; 16 emplacements au maximum ; noms
`[A-Za-z0-9_.:-]{1,200}` ; aucun autre champ.

Passer de 2 à 4 en ajoutant `dev-3` et `dev-4` garde `dev-1` et `dev-2`
configurés. L'inventaire durable `R/slots/inventory.json` refuse un emplacement
occupé dont le profil, le catalogue ou l'approbation change. Conformément au
plan, la passe 4 ne commence qu'après la fin et le nettoyage réels du travail
de la passe 2, ou sur de **nouveaux** tickets.

### 5.2 Par emplacement `<n>` (dev-1 … dev-4)

`R/slots/<n>/config/standing.json` :

```json
{"schema":"linux-cloud-queue-continuation-v1",
 "catalog_file":"/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/slots/<n>/config/catalog.json",
 "workflow_file":"/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/slots/<n>/config/workflow-standing.json",
 "approved_sha256":"<<À_REMPLIR:approval_sha256 du preview --workflow>>",
 "checkpoint_after_actions":3,"provider_backoff_seconds":60}
```

`R/slots/<n>/config/catalog.json` (`development-queue-catalog-v1`, voir
DEVELOPMENT-QUEUE.md) :

- `owner_id` `thescreamer`, `board_endpoint` `https://board.blackoutburst.com/mcp` ;
- `agent_id` et `profile_version` : `<<À_REMPLIR:codeur n>>`, distincts par
  emplacement ;
- `project_id` : `<<À_REMPLIR:projet bac à sable>>` ;
- `connection_file` : `R/slots/<n>/connections/coder.json` ;
- `timeout_seconds` au plus égal à 3510 (`lifetime_seconds` − 90) ;
- une entrée `entry_id` `t51-<n>`, avec la ressource distincte du ticket,
  `repository` `{repository_id:"agent-board", repository: R/slots/<n>/repositories/agent-board, base_commit:<<À_REMPLIR>>}`,
  des `paths` existants disjoints, `actions`
  `["repo.read_file","repo.patch_file","repo.run_test"]`, un vérificateur sous
  `R/slots/<n>/verifiers/` avec son sha256, `model` et `reasoning_effort`
  explicites (`<<À_REMPLIR:modèle de l'abonnement>>`, par exemple `low`) ;
- `planner_budgets` : `PlannerBudgets().document()`, soit 16 tours, 16
  actions et 1800 s ; `max_wall_seconds` doit rester au plus égal à
  `timeout_seconds`, et `checkpoint_after_actions` (3) au plus égal à
  `min(max_actions, max_turns)`. `broker_budgets` : `Budgets().document()`.

Le catalogue s'écrit comme dans DEVELOPMENT-QUEUE.md, sous l'UID propriétaire,
avec `PrivateDirectory(...).create`. On lance Python avec
`sys.path.insert(0, RELEASE + '/source')` depuis la release installée.

`R/slots/<n>/config/workflow-standing.json` (`linux-cloud-workflow-v1`) :
`batch_id` UUID modèle, un job `name` égal à `t51-<n>`, `paths` identiques à
l'entrée, et `commit_message`, `pr_title` et `pr_body` fixes. Le
`reviewer` porte `agent_id` (reviewer), `connection_file`
(`R/slots/<n>/connections/reviewer.json`, différent de celui du codeur),
`instruction`, `model`, `skills` et `timeout`.

`approved_sha256` s'obtient sans claim ni modèle, puis se recopie dans
`standing.json` :

```sh
RELEASE=/opt/blackoutburst-agent-pool/releases/$NEW_BUNDLE_ID
cd "$RELEASE/source" && sudo -u bb-agent-thescreamer -H /usr/bin/python3.10 -B -m blackoutburst_agents development-queue preview \
  --root "$R/slots/<n>" --catalog "$R/slots/<n>/config/catalog.json" \
  --workflow "$R/slots/<n>/config/workflow-standing.json" --session t51-<n>-preview
```

Ce preview ouvre la connexion de découverte du codeur. Il ne fait aucun claim
et aucun appel modèle ; la valeur retournée est calculée localement à partir
des empreintes. `cd` dans la release root sert uniquement à importer la source
publiée ; à adapter si le Lead préfère une autre invocation.

## 6. Diff d'unité (plafonds) : non applicable par le code actuel

Diff proposé, identique pour le service fini et le service de continuation :

```diff
-TasksMax=256
-MemoryMax=6442450944
-MemoryHigh=4294967296
-CPUQuota=300%
+TasksMax=1024
+MemoryMax=10737418240
+MemoryHigh=8589934592
+CPUQuota=600%
```

Le seul moyen prévu de produire ces lignes est le spec du paquet
(`unit_limits` : `tasks` 1024, `memory_bytes` 10737418240,
`memory_high_bytes` 8589934592, `cpu_percent` 600). Ces valeurs restent dans
les bornes de `package.limits()`. Elles ne peuvent pourtant pas être
**installées** sur l'existant :

- `upgrade` et `upgrade-continuation` exigent des `unit_limits` identiques à
  la release précédente ; sinon refus `pool_upgrade_scope_changed` ;
- `install` refuse une racine propriétaire ou une unité déjà présentes
  (`pool_existing_layout_refused`, `pool_unit_already_exists`) ;
- toute édition manuelle, tout drop-in, tout remplacement manuel de fichier
  d'unité ou de release et `systemctl set-property` sont **interdits** pour
  contourner ces gardes (et pour `pool_existing_layout_refused`). L'entrée compare les octets du fichier d'unité, et les mises à jour
  suivantes refusent un `DropInPaths` non vide.

Conséquence pour la recette :

- La passe 2 se fait **avec les plafonds actuels**, ce qui est cohérent avec
  « hypothèse jusqu'à mesure ».
- La passe 4 n'a lieu sous les plafonds actuels que si les mesures de la
  passe 2, extrapolées à quatre emplacements, restent nettement sous MemoryHigh
  4 GiB et TasksMax 256. Sinon, il faut d'abord un changement de code revu (par
  exemple une mise à jour arrêtée qui accepte explicitement un changement de
  `unit_limits` pour les trois unités), puis un nouveau bundle.
- Sur cet hôte de 15 GiB, MemoryMax 10G ne laisse qu'environ 5 GiB au reste du
  serveur. Le Lead valide cette valeur après mesure.

## 7. Commandes (pour un humain, après contre-revue)

Noms : `SVC=blackoutburst-agent-continuation-bb-agent-thescreamer.service`,
`TMR=blackoutburst-agent-continuation-bb-agent-thescreamer.timer`,
`PREV=66cce93319dbd72e392d3b5bb2495910dfc9a284762a9a6e0e87a5561a5f23be`.

### 7.1 Mise à jour arrêtée vers 0.50.0

```sh
systemctl show -p ActiveState,UnitFileState,DropInPaths "$SVC" "$TMR" blackoutburst-agent-pool-bb-agent-thescreamer.service
# attendu : inactive / disabled / vide pour les trois
python3 -I -B "$CAND/bundle/pool_manage.py" upgrade "$CAND/bundle" --previous-bundle-id "$PREV"
sudo /usr/bin/python3.10 -I -B "$CAND/bundle/pool_manage.py" upgrade "$CAND/bundle" --previous-bundle-id "$PREV" --expected-bundle-id "$NEW_BUNDLE_ID" --apply
RELEASE=/opt/blackoutburst-agent-pool/releases/$NEW_BUNDLE_ID
python3 -I -B "$RELEASE/pool_manage.py" upgrade-continuation "$RELEASE" --previous-bundle-id "$PREV"
sudo /usr/bin/python3.10 -I -B "$RELEASE/pool_manage.py" upgrade-continuation "$RELEASE" --previous-bundle-id "$PREV" --expected-bundle-id "$NEW_BUNDLE_ID" --apply
systemctl cat "$SVC" | grep -E 'ExecStart|TasksMax|Memory|CPUQuota'   # release = NEW, plafonds inchangés
```

Les deux commandes `--apply` laissent les unités désactivées. Elles ne lisent
pas la racine privée.

### 7.2 Mise en place des documents (unités arrêtées)

```sh
sudo -u bb-agent-thescreamer -H cp -p "$R/config/continuation.json" "$R/config/continuation.pre51.json"   # sauvegarde
sudo -u bb-agent-thescreamer -H /usr/bin/python3.10 -I -B "$RELEASE/pool_entry.py" --release "$RELEASE" --owner bb-agent-thescreamer continuation-plan --continuation "$R/config/slots-51-p2.json"
# Revue : configuration_sha256, concurrency, et par emplacement agent_id / project_id / entries / resources / sha.
sudo -u bb-agent-thescreamer -H install -m 0600 "$R/config/slots-51-p2.json" "$R/config/continuation.json"
sudo -u bb-agent-thescreamer -H /usr/bin/python3.10 -I -B "$RELEASE/pool_entry.py" --release "$RELEASE" --owner bb-agent-thescreamer continuation-plan --continuation "$R/config/continuation.json"
```

`install` écrit une copie et non un lien (nlink 1 exigé). Le plan affiche
`activated:false` et `launchable:false`.

### 7.3 Démarrage d'une passe

```sh
sudo /usr/bin/systemctl start "$TMR"   # start sans enable : pas d'activation au boot
```

Le timer lance une passe 30 s plus tard, puis 30 s après chaque fin de passe.
Une passe dure au plus 3510 s. Démarrer l'échantillonneur de la section 8
**avant** cette commande.

### 7.4 Stop (test manuel, puis arrêt normal)

Toujours arrêter le timer **d'abord**, sinon une nouvelle passe redémarre 30 s
plus tard :

```sh
sudo /usr/bin/systemctl stop "$TMR"
sudo /usr/bin/systemctl stop "$SVC"     # SIGTERM -> should_stop de chaque tick, puis Stop du Core ; TimeoutStopSec 45
systemctl show -p ActiveState,Result,ExecMainStatus "$SVC"
journalctl -u "$SVC" --since "-10min" --no-pager | tail -40
```

Test manuel pendant que les appels modèle se chevauchent : on attend une
sortie JSON `continuation-pool-slots-pass-v1` de statut `stopped`, avec les
lignes d'emplacement `stopped`/`not_started` ou le Core en état arrêté, et
**aucun** `continuation_pool_cleanup_unknown`. La reprise se fait avec
`systemctl start "$TMR"`. Les mêmes Cores doivent être rouverts, sans nouveau
claim ni nouvelle clé (à vérifier sur le Board). Le Stop Board d'une exécution
reste le chemin du Core existant ; ce module ne le rejoue pas.

### 7.5 Passage à 4

Après la fin réelle de la passe 2 et la vérification du nettoyage (section 10),
ou sur de nouveaux tickets, appliquer 7.4, puis 7.2 avec `slots-51-p4.json`,
puis 7.3. Ne jamais modifier `continuation.json` pendant une passe : le
changement arrête la passe (`continuation_pool_configuration_changed`).

### 7.6 Retour arrière

- Fonctionnel (retour au document de continuation mono-file) :
  **le `ContinuationPoolService` mono n'appelle pas `reconcile_inventory`.**
  Seul `QueueSlotsService` refuse une passe qui oublierait un pointeur
  d'emplacement occupé. Restaurer `continuation.pre51.json` peut donc
  abandonner silencieusement des emplacements encore actifs : il ne faut pas
  supposer que l'ancienne configuration refusera d'elle-même.
  Prévol obligatoire, vérifiable et consigné sur #51, **avant** tout
  remplacement :
  1. appliquer 7.4 : timer et service `inactive`, `$CG/cgroup.procs` vide ;
  2. **sous le bail propriétaire** : le bail est le `flock` exclusif de
     `R/continuations/owner.lock` (`ContinuationPoolService._lease`). L'humain
     le prend sans attente, ce qui échoue si une passe le tient, et fait la
     vérification **et** le remplacement dans le même bail :

     Le script est dans le dépôt :
     `integrations/codex-personal/recette51/preflight_76.py` (bibliothèque
     standard seule, revu avant usage ; testé par
     `tests/test_recette51_preflight.py`). L'humain en pose une copie lisible
     par le propriétaire depuis la source revue, puis vérifie qu'elle est
     identique octet pour octet à la tête revue :

     ```sh
     P=$R/config/preflight-76.py
     sudo install -o bb-agent-thescreamer -g bb-agent-thescreamer -m 0600 \
       "$CAND/source/integrations/codex-personal/recette51/preflight_76.py" "$P"
     sudo sha256sum "$P" "$CAND/source/integrations/codex-personal/recette51/preflight_76.py"   # identiques
     # 1) relevé seul (lecture seule) :
     sudo -u bb-agent-thescreamer -H flock -n -E 75 "$R/continuations/owner.lock" \
       /usr/bin/python3.10 -I -B "$P" --root "$R"             # 75 = bail occupé : arrêter
     # 2) après l'attestation de l'étape 3, dans un nouveau bail :
     sudo -u bb-agent-thescreamer -H flock -n -E 75 "$R/continuations/owner.lock" \
       /usr/bin/python3.10 -I -B "$P" --root "$R" --apply
     ```

     Sans `--apply`, le script ne fait que lire. Il parcourt sans suivre de
     lien **tous** les pointeurs `R/slots/*/admissions/queue-*/tick.json`
     **et** `R/admissions/queue-*/tick.json`. Chaque répertoire du chemin doit
     appartenir à l'UID effectif en 0700, chaque `tick.json` (et `tick.lock`)
     être un fichier régulier 0600 à lien unique. Sous `R/slots`, la seule
     entrée qui n'est pas un emplacement est l'inventaire qu'écrit
     `reconcile_inventory`, `R/slots/inventory.json` : fichier régulier 0600
     à lien unique de l'UID effectif, au plus 512 Kio, dont les octets sont
     exactement le JSON canonique du schéma
     `linux-cloud-queue-slots-inventory-v1` (`slots` : nom → `agent_id`,
     `catalog_sha256`, `approval_sha256`, `resources` triées). Il est lu,
     jamais modifié, et seul son sha256 est affiché. Tout lien symbolique,
     toute autre entrée inattendue (par exemple un `write-*` résiduel, un
     répertoire nommé `inventory.json`) ou tout autre type refuse (code 2).
     Il n'affiche que ce sha256, le chemin relatif, `phase` et
     `active_is_none`, puis `pointers=N all_idle=…`. Il sort en 0 seulement
     si tous sont `idle` avec `active` nul, sinon en 1. Avec `--apply`, il
     refait le même relevé, puis exige `config/continuation.pre51.json`
     privé, régulier et à lien unique. Il remplace alors atomiquement
     `config/continuation.json` par une copie (fichier temporaire 0600 dans
     le même répertoire, `fsync`, renommage, `fsync` du répertoire) et affiche
     les sha256 ancien et nouveau. Il ne supprime ni le document slots, ni
     les racines d'emplacement, ni aucun historique. Les deux relevés sont
     publiés sur #51 ;
  3. Board, par le Lead, **avant** l'étape 2 : aucune exécution Runtime
     ouverte ou incertaine, aucune réservation, aucun claim actif ou incertain
     sur les tickets des emplacements.
  Sinon : **rester arrêté** avec le document slots, ses racines
  d'emplacement et leurs historiques, et ouvrir un Needs-you. 0.50.0 conserve
  les schémas antérieurs (continuation, file standing, reprise de revue
  v1/v2), mais cela ne protège pas les pointeurs d'emplacement.
- Binaire : **aucun chemin de code ne revient à la release 0.49.1**.
  `upgrade` refuse une release déjà publiée (`pool_existing_layout_refused`),
  et `rollback` ne fait que supprimer le fichier de l'unité finie (reçu
  `installed_disabled`), en laissant les unités de continuation. Ne pas
  utiliser `rollback` ici. Un vrai retour exige un nouveau bundle (nouvelle
  révision) passé par `upgrade` et `upgrade-continuation`.

## 8. Mesures et preuves à collecter

L'échantillonneur est en lecture seule, lancé par un humain avec sudo (pour
`/proc/<pid>/cwd` d'un autre UID), toutes les 5 s :

```sh
SVC=blackoutburst-agent-continuation-bb-agent-thescreamer.service
CG=/sys/fs/cgroup/system.slice/$SVC
OUT=/home/ubuntu/recette-51/$(date -u +%Y%m%dT%H%M%SZ)-samples.log
install -d -m 0700 /home/ubuntu/recette-51
sudo bash -c 'while sleep 5; do ts=$(date -u +%FT%T.%3NZ)
  echo "$ts unit $(systemctl show -p ActiveState -p MemoryCurrent -p TasksCurrent -p CPUUsageNSec '"$SVC"' | paste -sd" ")"
  echo "$ts memevents $(paste -sd" " '"$CG"'/memory.events 2>/dev/null) pids $(cat '"$CG"'/pids.current 2>/dev/null)"
  for pid in $(cat '"$CG"'/cgroup.procs 2>/dev/null); do
    [ "$(cat /proc/$pid/comm 2>/dev/null)" = codex ] || continue
    echo "$ts codex pid=$pid ppid=$(ps -o ppid= -p $pid) start=\"$(ps -o lstart= -p $pid)\" etimes=$(ps -o etimes= -p $pid) rss_kb=$(ps -o rss= -p $pid) cwd=$(readlink /proc/$pid/cwd)"
  done; done' >> "$OUT"
```

Seuls le PID, l'heure, le RSS et le cwd sont relevés, pas les arguments : ils
peuvent contenir des consignes.

### 8.1 Portée exacte de ce qui est observable

L'échantillonnage à 5 s et les intervalles de processus `codex` **ne
prouvent pas** des appels modèle simultanés : un processus vivant peut
attendre, lancer des outils ou être en revue. Ils ne servent qu'aux mesures
de ressources et à la corroboration.

Ce que le backend installé (release 0.49.1) rend observable, d'après le code,
sans la télémétrie de 8.2 :

- chaque tour passe par `codex_turn.execute_turn` : un processus
  `codex exec --json` par tour, sans outil (tout item autre que
  `agent_message`/`reasoning` termine le tour en `unexpected_tool_event`).
  Le flux JSON porte `thread.started`, `turn.started`, `turn.completed`,
  mais **ni ces événements ni l'adaptateur n'enregistrent d'horodatage** ;
- `RunStore` (`turns`) ne garde que `created_at` (avant lancement) et
  `updated_at` (dernier changement : PID, puis règlement) : c'est une durée
  d'enveloppe de processus, pas une frontière d'appel ;
- le journal du planificateur ne garde que `started_at`/`deadline` de la
  génération ;
- aucune frontière d'appel HTTP au fournisseur n'est exposée par Codex
  0.163.0 à l'adaptateur.

Sans télémétrie dédiée, le critère « quatre appels modèle simultanés »
n'est pas acquérable : la recette ne pourrait publier que des
chevauchements d'enveloppes de processus.

### 8.2 Télémétrie bornée des tours codeur (implémentée, schéma `turn-telemetry-v2`)

Livrée sur la branche #51 (`d629cef`, `72d3519`, `fa9e5ca`, revue du Lead
favorable pour PR brouillon) ; détails et garanties de chaque couche dans
`CONTINUATION.md`, sections « Télémétrie bornée des tours (#51) » et
« Commentaires déplacés hors du roster natif (#51) ».

- Journal : `R/telemetry/turns-<uuid>.jsonl`, un par passe d'emplacements,
  privé (0700/0600), en ajout seul, `fsync` par enregistrement, schéma fermé,
  taille et nombre d'enregistrements bornés. Aucun prompt, réponse,
  raisonnement, argument, message d'erreur ni secret.
- Faits enregistrés par tour : `process_started`, `thread.started`,
  `turn.started`, le terminal (`turn.completed` / `turn.failed` / `error`),
  `cleanup_confirmed` ou `cleanup_failed` avec la **classe** de sortie
  (`zero`, `nonzero`, `signal`, `unknown`), puis un fait `result` fermé
  (statut, raison, session validée et les cinq booléens de vérification de
  `execute_turn`). Liaison : opération, ticket, `agent_id`, emplacement,
  `request_id`, pid, session.
- **Seuls les tours codeur** sont observés. Les tours reviewer
  (`review_pool`) et `team_role_pool` ne le sont pas ; le critère des quatre
  codeurs n'en a pas besoin.
- **Résultat final requis** : `turns()` ne déclare `completed` que pour la
  séquence exacte, avec une sortie `zero` et un `result` accepté par
  `execute_turn` (cinq booléens vrais), une identité constante et des temps
  monotones non décroissants. Un terminal reçu suivi d'un résultat refusé
  donne `failed`. Tout le reste (processus tué, sortie inconnue, nettoyage
  non confirmé, fait manquant, ordre ou identité incohérents) donne
  `uncertain`, jamais `completed`.
- Échec du journal : fermé (fail-closed). Le tour passe en `blocked` /
  `callback_failed`, puis la work en `needs_review` ; aucun rejeu, aucun
  succès fabriqué. Journal plein : refus avant prompt (`turn_telemetry_full`).
  Un disque ou un journal plein bloque donc les emplacements au lieu de les
  laisser tourner sans mesure.
- Portée : l'intervalle `turn.started` → terminal est un **tour Codex**
  (inférence plus surcoût interne du CLI), pas un appel HTTP isolé. Le
  rapport le dira ainsi ; aucune interception réseau.
- **Qualification native encore à faire.** Ces commits changent les sha de
  modules du roster natif (`codex_turn.py`, `dev_planner.py`,
  `development_worker.py`, `continuation_pool_service.py`,
  `development_queue.py`). Le paquet final doit être requalifié avec des
  sources et des pins frais, par l'opérateur humain autorisé, avant toute
  passe. Aucun paquet ni aucune qualification ne sont pré-acceptés.

### 8.3 Critère et recoupement

Critère de la passe 4 : quatre tours codeur `completed` au sens de
`turns()`, sur quatre emplacements, quatre tickets et quatre racines
distincts, dont les intervalles `turn.started` → `turn.completed` (temps
muraux) vérifient max(début) < min(fin), et aucun tour `uncertain` dans la
fenêtre. Passe 2 : même critère avec deux tours. Un tour `failed` ou
`uncertain` ne compte pas ; il est publié tel quel.

Recoupement (corroboration, pas preuve) :

1. échantillons de processus `codex` (PID, cwd `…/slots/<n>/checkouts/…` ou
   arbre `ppid` si bwrap masque le cwd), en séparant codeur et reviewer ;
2. Board : exécution Runtime (`lookup_execution`) avec début/fin, claim
   `claim_selected_queue_ticket` et sa clé, ticket, profil ;
3. `RunStore`/journaux du planificateur sous chaque `R/slots/<n>/` : seulement
   les colonnes d'horodatage, d'état et d'identifiants (jamais `result_json`
   ni contenu) ;
4. sortie JSON de chaque passe (`journalctl -u "$SVC"`) : `concurrency`,
   par emplacement `phase`, `request_id`, `logical_task_id`, et
   `waiting_capacity`/`resource_busy`.

À consigner avec la tête source, `NEW_BUNDLE_ID`, codex 0.163.0, gh 2.94.0,
le sha de Python, les `unit_limits` et les propriétés effectives
(`systemctl show -p CPUQuotaPerSecUSec,MemoryHigh,MemoryMax,TasksMax "$SVC"`).

Mesures à rapporter : MemoryCurrent maximal échantillonné, compteurs
`memory.events` (`high`, `max`, `oom`, `oom_kill`) avant et après, maximum de
TasksCurrent/pids.current, CPUUsageNSec par passe, RSS par processus codex.
Ces chiffres servent à décider des plafonds de la passe 4 (section 6).

## 9. Critères d'arrêt immédiat (appliquer 7.4)

- `memory.events` : `max`, `oom` ou `oom_kill` augmente ; ou `high` augmente
  de façon continue avec plus de 90 % de MemoryHigh.
- TasksCurrent au-delà de 230 (90 % de 256).
- Moins de 2 GiB disponibles sur l'hôte, ou swap en croissance.
- Un emplacement passe en `needs_you` ou `connection_expired`, ou on observe
  `continuation_pool_cleanup_unknown` ou `continuation_pool_configuration_invalid`.
- Un claim, une PR ou un fichier hors des quatre tickets bac à sable et de
  leurs chemins approuvés.
- Une erreur d'authentification Codex, signe possible d'une concurrence sur le
  `.codex` partagé, ou `paused_quota` sur le compte de TheScreamer. On s'arrête
  et on consigne, sans modifier l'auth.
- Une exécution Runtime ou un claim incertain sur le Board.
- Télémétrie : `turn_telemetry_full`, échec d'écriture du journal
  (`callback_failed`), ou un tour codeur `uncertain` dans `turns()`.

## 10. Nettoyage

1. Appliquer 7.4 et vérifier `ActiveState=inactive` pour le service et le
   timer, `UnitFileState=disabled` et l'absence de processus dans
   `$CG/cgroup.procs`.
2. Vérifier que chaque `R/slots/<n>/admissions/queue-*/tick.json` est en phase
   `idle` avec `active` nul (commande de la section 2, adaptée). Rien n'est
   effacé : les racines d'emplacement et les journaux restent comme preuves.
3. Board, par le Lead : aucune exécution Runtime ouverte ni réservation
   restante, aucun claim actif sur les tickets bac à sable, état final de
   chaque ticket (revue, et non accepté par défaut).
4. GitHub : les PR brouillon, leurs branches et les journaux historiques
   sont **conservés**. Leur fermeture ou suppression est une décision humaine
   distincte, prise après archivage, et **n'est pas** un prérequis de la
   preuve.
5. Laisser le document slots avec le timer arrêté par défaut. Le retour à
   `continuation.pre51.json` ne se fait qu'après le prévol de 7.6, sur
   décision du Lead.
6. Profils et appairages de test : archivés ou conservés par les humains.
7. Publier sur le ticket #51 les preuves de la section 8 et le résultat du
   nettoyage, en séparant ce qui est observé de ce qui est supposé.
