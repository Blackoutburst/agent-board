# Addon Linux : travaux et revues explicitement sélectionnés

Ce paquet ajoute une release indépendante sous
`/opt/blackoutburst-agent-pool/releases/<bundle_id>` et une unité
`blackoutburst-agent-pool-bb-agent-thescreamer.service`. L’installation conserve
les comptes existants et leur HOME passwd normal. Elle ne démarre, n’active,
ne modifie et ne redémarre aucun ancien Host. SDK, versions et inventaires
globaux restent gérés par la composition principale.

Le nouveau mode de l’unité est `run-workflow` : un à quatre codeurs explicitement
sélectionnés, commit testé, PR brouillon, preuve sur le ticket et avis d’un
reviewer distinct publié sur le Board. Le parcours historique `run-batch` reste
disponible et finit aux résultats privés du driver Development.
Les limites opérateur retenues sont CPU 300 %, MemoryHigh 4 GiB, MemoryMax 6 GiB,
TasksMax 256, RuntimeMaxSec 3600 et TimeoutStopSec 45. La Board doit autoriser les
profils/resources sélectionnés et disposer de capacité Runtime réellement suffisante ;
un ancien Runtime incertain continue de compter.

## Préparation et installation arrêtée

Les fonctions `prepare_addon`, `plan_addon`, `install_addon` et `rollback_addon`
sont dans `deploy.worker_pool.manage`. La préparation prend uniquement le
checkout Git public propre, les binaires Linux publics Codex, son bwrap adjacent
et gh staged, leurs versions, Python système exact avec son SHA, le compte Unix
existant, le propriétaire Board, l’endpoint et les limites explicites.
Elle copie ces outils dans la nouvelle release ; aucune authentification n’est
copiée. Le schéma `worker-pool-addon-v1` a son propre inventaire fermé.

Depuis le checkout public revérifié, avec `public-spec.json` contenant ces seuls
arguments explicites, les commandes sont :

```sh
python3 -B -m deploy.worker_pool.manage prepare --source "$CHECKOUT" --output "$BUNDLE" --spec public-spec.json
python3 -I -B "$BUNDLE/pool_manage.py" plan "$BUNDLE"
sudo /usr/bin/python3.10 -I -B "$BUNDLE/pool_manage.py" install "$BUNDLE" --expected-bundle-id "$APPROVED_BUNDLE_ID" --apply
```

L’identifiant approuvé doit être celui du plan revu. Les scripts publics et
l’inventaire doivent être revérifiés avant de leur accorder sudo. `install`
compare le document et l’identifiant sélectionnés avec la copie complète avant
sa publication. Il crée seulement la nouvelle release, les répertoires privés,
l’unité disabled/Restart=no et recharge les métadonnées systemd. Aucun UID n’est
créé. Les unités, aliases, drop-ins ou répertoires préexistants inconnus refusent
l’installation. Le reçu root est sous
`/var/lib/blackoutburst-agent-pool/installations/<bundle_id>.complete.json`.

```sh
sudo /usr/bin/python3.10 -I -B "$RELEASE/pool_manage.py" rollback "$INSTALL_RECEIPT"
sudo /usr/bin/python3.10 -I -B "$RELEASE/pool_manage.py" rollback "$INSTALL_RECEIPT" --apply
```

Le rollback vérifie que la nouvelle unité exacte reste inactive et disabled,
retire seulement son fichier et recharge systemd. Il conserve les données
privées, les releases, l’auth normale et toutes les autres unités.

## Configuration opérateur et premier lancement

La racine privée est
`/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer` avec `config`,
`connections`, `repositories`, `checkouts`, `git-state`, `verifiers` et `batches`.
Appairer explicitement les profils/connexions codeurs et reviewers sélectionnés par le flux existant,
directement dans `connections`. Fournir un nouveau dépôt local autorisé dans
`repositories` et des vérificateurs Python fixes approuvés dans `verifiers`.
La connexion normale du CLI sous ce même UID/HOME peut déjà être valable ; aucun
code de cet addon n’ouvre ou copie son auth. L’ancre Development canonique privée
du même UID doit exister avant le démarrage ; aucun journal Host/C0 n’est adopté.

Depuis la release publique installée, cette commande opérateur reproductible
crée seulement les répertoires canoniques absents, sous l’UID/HOME normaux :

```sh
sudo -u bb-agent-thescreamer -H /usr/bin/python3.10 -I -B "$RELEASE/pool_entry.py" --release "$RELEASE" --owner bb-agent-thescreamer init-development-anchor
```

Elle refuse les symlinks, propriétaires ou modes inattendus ; elle ne lit aucun
journal, `fence.lock` ou contenu d’évidence, ne change aucun élément existant et
n’en déduit aucune preuve de nettoyage.

`config/batch.json` est privé 0600. Le schéma fermé historique
`linux-finite-pool-batch-v1` garde exactement quatre `jobs` ; le nouveau
`linux-finite-pool-batch-v2` accepte de un à quatre travaux. Les deux contiennent
`batch_id` UUID, `owner_id`, `board_endpoint`, `timeout_seconds` (au plus RuntimeMaxSec−90).
Chaque job choisit explicitement `name`, `agent_id`, `connection_file`, le grant
Git complet `repository`, `project_id`, `ticket_id`, `paths`, `resource`, `actions`,
`verifier_path`, `verifier_sha256`, `test_name`, `test_timeout_seconds`,
`test_max_output_bytes`, `instruction`, `model`, `planner_budgets` et
`broker_budgets`. Les profils, tickets, connexions et ressources sont distincts.
Le `workspace_root` explicite est `checkouts/<batch_id>/<name>`. Rien n’est
sélectionné à partir d’un message, du contenu d’un ticket ou d’un modèle.

Le mode de l’unité utilise aussi `config/workflow.json`, privé 0600, pour les
chemins de publication, textes de PR et le reviewer distinct de chaque travail.
Le [contrat complet](../../integrations/codex-personal/CLOUD-WORKFLOW.md) décrit
le schéma et le plan `workflow-plan`. Une première mise en service peut donc
choisir un codeur et un reviewer. La destination GitHub fixe exige la connexion
officielle gh normale du même compte, avec droit d’écriture, sans copie d’auth.

Les entrées publiques `batch-plan` et `workflow-plan` affichent les SHA de
configuration et les sélections sans qualification ni RPC. Un démarrage humain
explicite de la nouvelle unité est nécessaire. Les deux modes d’exécution
exigent la release/unité root exactes,
le compte normal et son cgroup systemd courant. Préparation, qualifications
natives et exécution ont lieu dans cette même unité. Aucune preuve native UID1000
ou qualification effectuée hors unité n’est transférée.

Une intention durable précède les effets. Après les UUID fixés par submit,
tous les préparateurs concrets doivent confirmer leurs retours et leurs reçus
complets épinglés avant une unique intention de dispatch. Une perte de réponse
après binding refuse tout dispatch. Un redémarrage lit seulement l’historique
existant ; aucune interruption ne rejoue submit, préparation, qualification ou
run. Le handler SIGTERM pose un Event ; le compagnon persiste Stop au Pool.
`shutdown(30)` ne constitue jamais une preuve de nettoyage : les travaux encore
actifs ou retenus restent inconnus/chargés et le lot échoue. Le délai systemd de
45 secondes peut imposer l’arrêt du groupe ; aucune fausse fin Runtime n’est
produite. La réconciliation explicite des opérations existantes reste nécessaire.

## Lecture de l’autorisation et mise à jour arrêtée

Le SDK 0.34 exige `get_development_ticket_frame` pour les travaux Development
autonomes. Cette requête bornée rassemble les observations de permissions,
ticket et historique. Elle conserve les vérifications avant/après et les
versions ; elle ne constitue ni une transaction atomique ni une autorisation
réutilisable. Un outil absent ou une réponse invalide arrête le parcours avant
le modèle. Le délai de contrôle natif de 1,5 seconde reste inchangé. Les familles
conservent leur protocole existant.

La commande `upgrade` vérifie le reçu root précédent et l’unité arrêtée. Elle
exige le même compte, HOME, propriétaire Board, endpoint, outils, limites et
modèle d’unité. Seule la release publique change. Les métadonnées de la racine
privée sont vérifiées sans lire son contenu ; connexions, configurations et
anciens lots sont conservés. Aucun démarrage n’est effectué.

```sh
python3 -I -B "$BUNDLE/pool_manage.py" upgrade "$BUNDLE" --previous-bundle-id "$PREVIOUS_BUNDLE_ID"
sudo /usr/bin/python3.10 -I -B "$BUNDLE/pool_manage.py" upgrade "$BUNDLE" --previous-bundle-id "$PREVIOUS_BUNDLE_ID" --expected-bundle-id "$APPROVED_BUNDLE_ID" --apply
```

Une unité `failed` est acceptée uniquement avec PID et ControlPID nuls, disabled,
sans drop-in et avec les octets précédents exacts. Après remplacement, elle
reste inactive. Un échec avant le reçu complet restaure uniquement une unité
reconnue. Une perte de confirmation du reçu laisse la nouvelle unité arrêtée
dans un état à examiner, sans adoption ni rejeu. Un lot interrompu exige une
nouvelle sélection explicite avec un nouvel UUID.
