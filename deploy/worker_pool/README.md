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
arguments explicites, les commandes sont ci-dessous. La fabrication offline
non root utilise le module du checkout public revu ; elle conserve son contexte
package. Tous les scripts standalone du bundle publié, dont le manager et
l'entrée d'exécution, s'invoquent avec `-I -B`. Revérifier les octets exacts du
manager **et de son fichier adjacent `pool_package.py`** avant tout sudo ; un
répertoire candidat ou un cache Python ne constitue pas une source approuvée :

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
Depuis 0.40, un job peut aussi fixer `reasoning_effort` à `low`, `medium`, `high`
ou `xhigh`. Le plan l'affiche et la sélection le conserve jusqu'au CLI, y
compris lors d'une reprise de session. Omettre ce champ conserve le contrat
historique ; une valeur non prise en charge est refusée sans repli automatique.
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
modèle d’unité. L’unique exception est le passage exact du modèle livré en
0.35.0/0.36.0 vers 0.37.0/0.37.1, qui ajoute `AF_NETLINK` pour la préparation réseau
de bubblewrap. Toute autre différence de modèle reste refusée ; les octets
relus doivent correspondre aux empreintes des deux manifestes. Les métadonnées de la racine
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


## Continuation explicite : installation inactive puis activation humaine

Le SDK 0.45 ajoute le service séparé
`blackoutburst-agent-continuation-bb-agent-thescreamer.service` et son timer.
Ils utilisent les mêmes plafonds d'unité et le même HOME normal, avec
`Restart=no`. Le timer programme un passage après 30 secondes au boot puis
30 secondes après l'inactivité du service ; `Persistent=false` ne rattrape pas
les déclenchements manqués. La sélection reste celle des seuls documents
opérateur, sans drainage de la file générale.

Le bundle public exact doit déjà être installé et reçu. L'unité finie doit être
arrêtée, désactivée, sans drop-in et conforme à sa release. Après vérification
du manager et de `pool_package.py`, l'installation additionnelle est :

```sh
sudo /usr/bin/python3.10 -I -B "$RELEASE/pool_manage.py" install-continuation "$RELEASE" --expected-bundle-id "$APPROVED_BUNDLE_ID" --apply
```

Cette commande ajoute uniquement les nouveaux fichiers service/timer désactivés
et recharge systemd. Elle ne crée ni configuration, ni journal logique, ni
connexion ; une intention ou une installation partielle reste à examiner.
Ne pas précréer `continuations` sous la racine privée, même vide : le service
initialise lui-même cet état neuf après les contrôles.

Fournir explicitement le [fichier continuation](../../integrations/codex-personal/CONTINUATION.md)
privé 0600 et les nouveaux batch/workflow auxquels il renvoie. Les anciens
fichiers et lots restent historiques. Lire ensuite le plan sans modèle :

```sh
sudo -u bb-agent-thescreamer -H /usr/bin/python3.10 -I -B "$RELEASE/pool_entry.py" --release "$RELEASE" --owner bb-agent-thescreamer continuation-plan --continuation "$POOLROOT/config/continuation.json"
```

Ce plan ne qualifie et n'active rien. Après revue humaine des sources, profils,
tickets, chemins, tests, budgets et du plan, une étape distincte peut activer
le timer maintenant **et au démarrage du serveur** :

```sh
sudo /usr/bin/systemctl enable --now blackoutburst-agent-continuation-bb-agent-thescreamer.timer
```

Une réponse perdue après cet effet n'autorise aucun nouvel essai automatique.
Stop, les baux et Runtime restent contrôlés ; un nettoyage inconnu ou un refus
de permission ne se transforme pas en checkpoint rejouable. Le parcours source
est composé, mais le pilote réel 0.45 à deux générations reste à valider.

### Mettre à jour une continuation existante

Pour une nouvelle release, arrêter **et désactiver** le service et le timer de
continuation sélectionnés avant toute mise à jour. La commande `upgrade` met
d'abord à jour l'unité finie et installe le nouveau bundle public arrêté. Puis,
après vérification des deux scripts publics, remplacer les unités de continuation :

```sh
python3 -I -B "$NEW_RELEASE/pool_manage.py" upgrade-continuation "$NEW_RELEASE" --previous-bundle-id "$PREVIOUS_CONTINUATION_BUNDLE_ID"
sudo /usr/bin/python3.10 -I -B "$NEW_RELEASE/pool_manage.py" upgrade-continuation "$NEW_RELEASE" --previous-bundle-id "$PREVIOUS_CONTINUATION_BUNDLE_ID" --expected-bundle-id "$APPROVED_BUNDLE_ID" --apply
```

Les comptes, endpoint, outils, plafonds et trois modèles d'unités doivent être
identiques. L'ancien reçu complet et les anciens octets service/timer sont
vérifiés sous le verrou Root commun ; le nouveau reçu de l'unité finie doit
correspondre à son installation arrêtée. La commande sauvegarde les deux fichiers
d'unités dans les métadonnées Root, les remplace, recharge systemd et réinitialise
seulement l'état failed du service de continuation. Ils restent désactivés ;
aucun contenu de la racine privée ou des connexions n'est lu ou modifié.

Un échec avant l'écriture du reçu complet restaure uniquement les octets encore
reconnus. Une modification externe ou une confirmation de reçu incertaine reste
à examiner ; la même intention ne peut pas être rejouée. Le reçu complet garde
le format de l'installation initiale pour permettre une mise à jour ultérieure.
Cette opération ne réactive pas le timer et ne reprend aucune génération : une
éventuelle activation humaine reste une étape distincte après validation.
