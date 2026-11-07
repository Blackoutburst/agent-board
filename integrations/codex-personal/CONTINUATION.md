# Reprise du travail par cycles — adaptateur 0.50.0

Entre deux invocations du même Core, seuls les identifiants du mapping des
namespaces Linux peuvent changer. L'ancien plan reste une preuve historique
liée à son hash, à ses outils, sources et répertoires ; il n'est pas rouvert
comme autorisation d'exécution actuelle. La nouvelle génération qualifie
intégralement son backend et sa politique dans son propre environnement.
Les champs host, UID/GID, cgroups, capacités, seccomp et label restent égaux,
ainsi que les outils, sources, scripts, permissions et plafonds approuvés.

La source 0.48 ajoute `development-queue tick` : une commande ponctuelle qui
admet au plus une tâche depuis un catalogue et un workflow opérateur approuvés,
puis appelle le service de continuation existant. L'approbation porte sur les
fichiers et plafonds permanents, pas sur une liste volatile de tickets. Le
catalogue doit sélectionner un ticket explicitement assigné au codeur ; aucune
assignation ou permission n'est créée automatiquement. Aucun nouveau service,
timer, modèle ou compte n'est installé ou activé par cette commande.

Le pointeur privé `admissions/queue-<owner-agent-hash>/tick.json` conserve une
seule admission active par profil. Les passes suivantes rouvrent le même Core,
sans découverte supplémentaire ni appel à `FiniteLinuxPoolBatch.run`. Un état
incertain reste attaché à sa demande ; un Core en `needs_you`, arrêté ou pending
ne permet pas de choisir une tâche de remplacement. Après un avis indépendant
confirmé, le reçu fermé est archivé avec création exclusive avant le retour à
idle ; cet avis ne marque ni le ticket ni le projet acceptés. Une autre tâche
ne peut être découverte qu'à la passe suivante.

Chaque génération garde son admission de file immuable. La première utilise
la version du batch v3 ; les suivantes utilisent uniquement la version exacte
du reçu de retraite/libération, relue sur un ticket Todo sans claim et toujours
assigné au codeur. Le profil, le catalogue, la base publique d'origine et les
plafonds restent fixes. Chaque génération crée de nouveaux opération/SID/work
et une nouvelle clé de claim sélectionnée ; un candidat `prepared_waiting`
conserve sa version, même après redémarrage. Aucun ancien compteur, fence,
travail ou budget n'est réinitialisé. `max-seconds` borne une passe seulement.

Les pauses fournisseur conservent le checkpoint et le délai de backoff. Après
ce délai, une disponibilité locale fraîche et les contrôles d'autorité sont
nécessaires avant préparation, puis à nouveau avant admission. Une interruption
ambiguë ne devient jamais une reprise implicite. Les tests temporaires utilisent
le vrai Board, Core, Worker et Runtime avec transports/qualification simulés ;
aucun nouveau pilote réel de file persistante n'est revendiqué ici.

L'unité déjà installée peut appeler cette file sans changer son point d'entrée.
Le document sélectionné par `--continuation` utilise alors le schéma distinct
suivant, écrit et approuvé hors du modèle :

```json
{
  "schema": "linux-cloud-queue-continuation-v1",
  "catalog_file": "/chemin/prive/pool/config/catalog.json",
  "workflow_file": "/chemin/prive/pool/config/workflow-standing.json",
  "approved_sha256": "<hash retourné par preview --workflow>",
  "checkpoint_after_actions": 3,
  "provider_backoff_seconds": 60
}
```

`continuation-plan` valide les documents locaux et affiche leurs empreintes avec
`execution_qualified=false`, sans ouvrir la connexion ni appeler le Board.
`run-continuation-once` passe par la même façade `ContinuationPoolService`, puis
par le client de découverte normal et un seul `tick`. La session de découverte
est déterministe pour la configuration et le profil ; les sessions de chaque
génération restent neuves. Le Core généré conserve l'ancien schéma, ce qui
sépare explicitement sa reprise de la découverte et évite toute récursion.
L'ancien schéma d'une tâche Core sélectionnée garde son parcours de reprise.

## Plusieurs emplacements de développement (#51)

La source 0.50 permet à une passe de remplir jusqu'à N emplacements libres.
Chaque emplacement compose une file standing existante ; aucune nouvelle
frontière d'admission n'est créée. Le document opérateur, sous `config` de la
racine propriétaire, ne contient que la liste et le plafond :

```json
{
  "schema": "linux-cloud-queue-slots-v1",
  "concurrency": 4,
  "slots": [
    {"name": "dev-1", "continuation_file": "/RACINE/slots/dev-1/config/standing.json"},
    {"name": "dev-2", "continuation_file": "/RACINE/slots/dev-2/config/standing.json"}
  ]
}
```

La racine d'un emplacement est toujours `RACINE/slots/<name>` ; elle n'est
jamais lue dans le document. Elle contient ses propres `config`,
`connections`, `repositories`, `checkouts`, `verifiers`, admissions, Core,
journaux et verrou. Son `continuation_file` est un document
`linux-cloud-queue-continuation-v1` normal, avec un catalogue lié à un profil
codeur distinct : le board n'accepte qu'un travail de file non terminé par
profil. `concurrency` vaut 1 à 8 et au plus le nombre d'emplacements (16
maximum). Ce plafond est un contrôle local du propriétaire ; il ne prouve
aucune capacité fournisseur ou abonnement. Les schémas existants et leur
`concurrency: 1` sont inchangés.

Une passe prend le verrou de la racine propriétaire, relit les pointeurs
durables `tick.json` de chaque emplacement, puis lance au plus N `tick` en
parallèle, chacun dans son fil, avec sa session de découverte. Les emplacements
qui tiennent déjà un travail passent d'abord et rouvrent le même Core, sans
découverte ni nouvelle clé de claim. Un emplacement libre ne découvre que si les
emplacements occupés, comptés depuis ces pointeurs et non depuis la mémoire,
restent sous le plafond ; sinon il reste `waiting_capacity`, sans RPC. La
découverte d'un emplacement libre ne voit pas les tickets dont la ressource est
tenue ou réservée par un autre emplacement ; cette exclusion est figée pour son
`tick`, avant toute intention. S'il ne reste rien, l'emplacement affiche
`resource_busy` et son pointeur reste idle. Une admission interrompue sans
intention lisible tient prudemment toutes les ressources de son catalogue. Le
claim atomique `claim_selected_queue_ticket`, les conflits de ressource du board
et les admissions Runtime restent les autorités finales. Le module
`development_queue_slots` reste hors des listes de sources natives Linux ;
l'entrée installée y route ce schéma à partir d'un paquet 0.50.0.

Stop, l'échéance de la passe ou un changement du document atteignent chaque
emplacement par le `should_stop` de son propre `tick`, puis par le Stop
existant du Core, du Worker et des tests. Un redémarrage relit chaque pointeur :
une admission `admitting` reprend sa demande, un Core `active` est rouvert ;
aucun ticket n'est réclamé, lancé ou publié une seconde fois par cette couche.
Une file vide ou un plafond atteint n'appelle aucun modèle. Les pauses
`paused_quota`/`paused_provider` restent propres à leur emplacement, avec leur
`next_observation_at` ; les autres continuent. Une connexion refusée
(`authentication_failed`, `permission_denied`) devient `connection_expired` et
demande une action humaine ; une panne de transport devient
`connection_unavailable`. Dans les deux cas, le pointeur de l'emplacement n'est
pas modifié et la passe suivante réessaie la même frontière. Ces deux états ne
concernent que la connexion de découverte de l'emplacement : une erreur board
levée dans le Core, par exemple un claim sélectionné refusé ou sans réponse,
reste `needs_you` avec sa preuve, et la passe suivante rouvre le même Core et la
même tâche logique, sans redécouverte ni claim de secours.

Portée de Stop vérifiée par les tests : avant la passe, pendant la lecture de
découverte (l'intention enregistrée `admitting` est reprise telle quelle), pendant
l'admission (`admit` n'a pas de point d'arrêt propre : l'admission se termine,
puis le `tick` s'arrête avant le Core), et pendant les phases modèle et test du
Core simulé. Stop pendant un vrai appel modèle ou un vrai test passe ensuite par
le Stop existant du Core, non rejoué ici.

Avant toute découverte, et sous le même verrou, la passe relit tous les
pointeurs de file de la racine propriétaire, pas seulement ceux du document :
`RACINE/admissions/queue-*` (une continuation standing seule sur cette racine)
et `RACINE/slots/*/admissions/queue-*`. Si un pointeur tient encore un travail
qu'aucun emplacement configuré ne reprendra (emplacement retiré, profil changé,
continuation seule), la passe entière est refusée avec
`continuation_pool_configuration_invalid`, sans claim ni appel modèle. Il en va
de même si un emplacement occupé change de liaison (profil, catalogue ou
approbation), d'après l'inventaire durable `RACINE/slots/inventory.json`. Un
lien symbolique ou une entrée inattendue sous `RACINE/slots` est aussi refusé.
Rien n'est réparé ni effacé : l'opérateur remet l'ancien document ou laisse le
travail se terminer. Un emplacement en pause ou `needs_you` reste ainsi compté
dans le plafond jusqu'à ce que son pointeur redevienne idle ; il est visible
dans la ligne de son emplacement.

Les tests `test_queue_slots.py` utilisent le vrai `DevelopmentQueueAdmission`,
ses intentions, reçus et pointeurs, avec une découverte board et un Core
simulés en mémoire. Ils couvrent quatre emplacements simultanés, le plafond, le
conflit de ressource, Stop, la reprise, la file vide, le quota et la connexion
expirée. **La recette réelle avec quatre modèles d'abonnement simultanés n'a pas
été exécutée** : aucun résultat synthétique ne vaut preuve d'abonnement ou de
capacité fournisseur.

Pour ces workflows complets, `checkpoint_after_actions` doit être au moins 2 :
le test nommé et l’action finale doivent pouvoir se suivre dans une génération.
Une fenêtre de 1 céderait après chaque test, puis la génération suivante devrait
refaire le test sans jamais atteindre la preuve finale fraîche. Les bornes
supérieures et les budgets ne changent pas. Les checkpoints partiels génériques
`DevelopmentCheckpointRequest(after_actions=1)` restent valides.

Pendant la préparation d'une génération suivante, chaque poll natif collecte
sa propre autorisation fraîche et revalide les pins, l'identité, le Stop et
l'état Core local. Les contrôles complets du cycle restent aux frontières de
préparation, sans seconde collecte distante dans la fenêtre de contrôle native.
La durée de validité reste de 1,5 s et toute échéance dépassée refuse. Aucun
état pending ni préparation incertaine n'est rouvert.

Le 2 novembre, le pilote 0.45.5 #65 a confirmé deux générations auteur, la PR
#82 et un avis distinct publié, avec sortie propre des opérations sélectionnées.
Cet avis reste `limited` : les fichiers complets et les métadonnées détaillées
des tests manquaient au reviewer. La source 0.45.6 complète ce matériau borné
et conserve les seals historiques ; sa recette réelle exige un nouvel essai.
Ce résultat ne valide pas une panne de quota réelle, le parallélisme #51 ou
la hiérarchie #52.

Le ticket #60 ajoute une tâche logique durable, séparée des générations de
travail. La tâche n'a aucun plafond de cycles ou de durée totale. Chaque
génération garde les plafonds approuvés du planificateur, du broker, des tests
et de l'unité serveur. La source 0.45 compose les frontières de cycle,
l'indisponibilité fournisseur, l'agrégat final, la revue distincte et le service
borné. Le récit ci-dessous conserve les étapes et refus des anciens essais.
Le pilote fini 0.44.2 a réussi avec nettoyage confirmé le 29 octobre 2026 et sa
PR #74 a été fusionnée. Le premier pilote de continuation #62 a confirmé sa
lecture et son patch, puis s’est arrêté au contrôle du résultat du test Linux
avec `linux_result_boundary_invalid`. Le timer a été arrêté par le propriétaire ;
la génération et sa source restent conservées. La réconciliation normale a
ensuite confirmé le nettoyage des six réservations Runtime ; le test reste
non validé et le Core historique reste en `needs_you`. La recette à deux générations réelles, publication et revue reste
à valider. La version 0.45.1 sépare les contrôles locaux de Stop/identité/délai
des observations complètes aux frontières d’action ; les autorisations natives,
les captures SOURCE et la fenêtre de 1,5 seconde restent obligatoires.

Le diagnostic du pilote 0.45.1 a ensuite confirmé deux générations, le premier
checkpoint et sa retraite, puis un deuxième Worker `result_ready` avec un
planificateur `review_ready`. Le Core est resté en `needs_you` à la finalisation ;
les douze reçus Runtime locaux étaient terminés avec nettoyage confirmé, et
le contrôle serveur a permis la libération normale de la claim expirée. Le
timer est arrêté et ce Core historique reste conservé.

La version 0.45.2 prend en charge une dernière génération qui vérifie le travail
déjà sauvegardé sans ajouter de patch. Sa capture intermédiaire reste privée et
vérifie les fichiers et les tests ; le delta final publié reste calculé contre
la base publique d’origine et doit être non vide. Un scénario synthétique a
reproduit le refus antérieur après `read → test → final` ; le parcours serveur
complet avec publication et revue distincte reste à valider.

Le 30 octobre 2026, le pilote 0.45.2 a repris son checkpoint dans une seconde
session, validé la postimage attendue et publié la PR brouillon #77. La passe
s'est ensuite arrêtée après vingt minutes pendant la revue ; aucun avis final
ni nettoyage final du reviewer n'est encore confirmé. Ce résultat ne valide
pas le parcours complet et l'ancien essai n'est pas relancé.

La version 0.45.3 rend linéaire l'accumulation des octets dans la lecture
protégée des fichiers. Elle évite les copies répétées du préfixe des gros
exécutables, tout en gardant les mêmes octets retournés, limites, descripteurs,
observations d'identité et empreintes complètes. Les droits, contrôles de
source, délais et règles de reprise restent identiques. La mesure locale sur
un fichier synthétique de 64 Mio passe de 5,326 s à 0,030 s ; elle ne démontre
pas la cause exacte de l'arrêt serveur ni une validation réelle de cette version.

Le pilote 0.45.3 a ensuite conservé son premier checkpoint et retiré sa première
génération avec six reçus Runtime locaux terminés et nettoyés. La préparation
de la génération suivante a refusé la qualification `controller_loss`, avant
tout nouveau Work ou Runtime. Le service et le timer sont arrêtés ; le Core,
les générations et la préparation interrompue restent historiques. Une
qualification réelle des neuf cas sous Ubuntu UID1000 a réussi. Une pause
contrôlée de 1,2 seconde après `fault_ready` reproduit la signature du refus,
mais ces essais ne prouvent pas sa cause dans le compte de production UID994.

La version 0.45.4 conserve dans le résultat public du passage la phase, la
catégorie, le code public connu et les UUID déjà disponibles d'un refus de
cible. Elle y ajoute l'observation fermée de qualification lorsqu'elle existe.
Le protocole du test distingue aussi les refus de l'observateur, du processus
enfant et de la relecture d'état avant ACK. L'erreur de l'enfant garde seulement
son étape et un code autorisé ; aucun texte d'exception, chemin privé, prompt
ou résultat modèle n'est publié. Les contrôles, délais, conditions de succès,
nettoyage et règles de non-rejeu restent identiques. Cette correction rend le
prochain refus observable ; elle ne valide pas encore le parcours réel complet.

Le pilote serveur 0.45.4 a confirmé le premier checkpoint, une seconde
opération de développement et la publication de la PR brouillon #80. Un
reviewer distinct a pris le ticket #64, puis le service a signalé
`continuation_review_cleanup_unknown`. Aucun avis final n'est confirmé ;
la PR et les états locaux sont conservés. Le contrôle normal du serveur a
libéré le bail reviewer expiré, sans compléter le pilote ni réécrire ses reçus.

Le profilage d'un scénario composé à deux générations a mesuré 533 validations
complètes de la source publiée et 6 831 commandes Git. La version 0.45.5
regroupe les seules lectures de publication GET/`ls-remote` d'un appel `status`
entre deux validations complètes de cette source. Chaque commande conserve
ses contrôles frais de permissions, de Stop, d'identité, d'arguments et
d'outils. Le contexte est privé, lié à la source et au thread, puis révoqué
en sortie ; aucune observation ou autorisation n'est réutilisée entre appels.
Les lecteurs utilisés hors de ce contexte conservent la validation complète.

Le même scénario profilé passe de 173,04 à 81,47 secondes, de 533 à
148 validations complètes et de 6 831 à 3 366 commandes Git. Les 385 contrôles
intermédiaires conservent leurs vérifications fraîches d'autorisation ; les
55 observations de publication sont inchangées. Ce scénario utilise de vrais
composants temporaires Board/Git/Runtime avec modèles, transport GitHub et
qualifications natives simulés. Ces mesures ne valent pas validation du
nouveau parcours réel sur le serveur.

Un cycle propre conserve le checkout, les actions confirmées, leurs reçus et
la filiation Git dans un checkpoint privé. Le contrôleur termine et vérifie
le nettoyage Runtime, puis libère normalement sa propre réservation de ticket.
La génération suivante reçoit un nouveau journal, une nouvelle session et une
nouvelle admission. Les fichiers d'un cycle historique restent inchangés.

Si le fournisseur devient indisponible, le checkpoint et l'état de pause sont
conservés. Le contrôleur attend le backoff puis relit une observation fraîche
avant une nouvelle préparation. Il vérifie encore la disponibilité avant
l'admission. Aucun appel modèle périodique, changement de compte ou changement
de modèle ne sert à tester le retour du quota. Une disponibilité inconnue
ne vaut pas une autorisation.

Une action dont le résultat est incertain, un nettoyage non confirmé ou une
permission manquante exige une intervention. Une réponse humaine ne réécrit
pas les reçus, n'efface pas Stop et ne rend pas une opération incertaine
rejouable. L'état local `needs_you` reste distinct d'une demande humaine
confirmée par le board. Le raccord de visibilité peut poser une question fixe
sur les permissions ou les effets seulement avec le vrai Worker/Core vivant,
une claim propre encore courante, la source et le fence frais, et Runtime
positivement nettoyé. Il conserve une intention puis retrouve l'ACK sans
renvoyer une question incertaine. À défaut, l'avis fermé existant exige encore
le droit de réponse ; un refus reste localement en attente. Une pause propre,
le backoff et Stop ne déclenchent pas cette alarme. Les réponses dans Needs you
ne rouvrent pas le Core et ne constituent aucun grant d'exécution.

## Télémétrie bornée des tours (#51)

Chaque passe d'emplacements tient un journal privé, en ajout seul, des
frontières de tours Codex des **codeurs** : `R/telemetry/turns-<uuid>.jsonl`
(répertoire 0700, fichier 0600, ouvert `O_APPEND|O_NOFOLLOW|O_CREAT`,
verrou exclusif, `fsync` par enregistrement). Le fichier n'est créé qu'au
premier enregistrement : une passe sans tour modèle ne laisse rien. Le nom
du fichier et ses compteurs figurent dans la sortie JSON de la passe
(`telemetry`).

Chemin, du code hors roster vers le code du roster, sans import inverse :
`development_queue_slots` (hors roster) crée le journal et passe un
enregistreur par emplacement à `DevelopmentQueueAdmission.tick(turn_event=…)`,
qui le pose sur `ContinuationPoolService.turn_event`, puis
`_Generation.open_worker` sur `DevelopmentBoardWorker.turn_event`. Le Worker
le transmet à `DevelopmentPlanner(_turn_event=…)` seulement s'il est posé,
et le planificateur l'attache à `execute_turn(on_turn_event=…)` avec l'ID de
ticket de sa portée et le `request_id` du pas courant.
`turn_telemetry.py` reste hors de `LINUX_EXECUTION_SOURCES` et aucun module du
roster ne l'importe. Les tours du reviewer (`review_pool`) ne sont pas encore
raccordés ; le schéma réserve `reviewer`/`planner`.

`execute_turn` n'appelle le rappel que sur des faits validés, avec un
dictionnaire fermé `{kind, wall, monotonic, session_id}` :
`process_started` (après `on_started`, avant tout octet du prompt),
`thread.started` (session validée), `turn.started`, le terminal
(`turn.completed`, `turn.failed`, `error`) et `cleanup_confirmed` ou
`cleanup_failed` après l'arrêt du groupe. Aucun texte du modèle, prompt,
raisonnement, argument, message d'erreur ou secret n'y passe. Un événement
mal formé, dupliqué ou hors ordre est refusé par le flux existant et n'est
jamais signalé.

Échec : **fermé**. Si le rappel lève (refus, journal plein, disque plein,
écriture courte, fichier non sûr), `execute_turn` suit la sémantique
existante des rappels : arrêt du groupe de processus, tour `blocked` avec
`callback_failed`. `RunStore` garde un tour lancé et non réglé comme
`uncertain` et passe le travail en `needs_review` ; la même requête renvoie
ensuite son résultat en cache sans relancer le transport. Une télémétrie
défaillante ne peut donc ni rendre un tour incertain réussi, ni rejouer son
appel. Seule exception : sur un terminal `error`/`turn.failed` hors mode
`provider_disposition`, le tour échoue déjà ; sa catégorie d'erreur est
conservée au lieu d'être remplacée par `callback_failed`. Après une écriture
ratée, le journal refuse tout nouvel enregistrement (`turn_telemetry_unavailable`)
: on n'ajoute jamais après une queue déchirée ou non synchronisée.

Bornes : 1 024 octets par enregistrement, 16 384 enregistrements et 8 Mio
par fichier. Un nouveau tour (`process_started`) n'est admis que s'il reste
256 enregistrements de réserve pour les tours déjà commencés ; sinon le
journal écrit une fois un marqueur `journal_full` et refuse explicitement
(`turn_telemetry_full`), avant tout envoi de prompt.

Lecture : `read_journal` (borné, sans réparation, signale une queue
déchirée) et `turns` (intervalles par tour). Un tour n'est `completed` que
s'il a exactement un terminal `turn.completed`, un `turn.started` et un
`cleanup_confirmed`, sans doublon ; un terminal d'échec donne `failed` ;
tout le reste (terminal absent, processus tué, nettoyage non confirmé) reste
`uncertain`. Une reprise peut ajouter `correlate(seq, raison)` qui référence
un enregistrement antérieur ; elle ne fabrique jamais de terminal.

Les horloges monotones ne se comparent qu'au sein d'un même processus
(`pid` est enregistré) ; toutes les places d'une passe tournent dans le même
processus. L'intervalle `turn.started` → terminal reste un tour Codex, pas
un appel HTTP isolé.

## Sélection explicite

Le nouveau fichier, propriétaire et privé comme les deux fichiers du
[workflow](CLOUD-WORKFLOW.md), lie chaque travail à une identité logique stable :

```json
{
  "schema": "linux-cloud-continuation-v1",
  "batch_file": "/PRIVATE_OWNER_ROOT/config/batch.json",
  "workflow_file": "/PRIVATE_OWNER_ROOT/config/workflow.json",
  "concurrency": 1,
  "provider_backoff_seconds": 30,
  "tasks": [{
    "name": "developer",
    "logical_task_id": "00000000-0000-4000-8000-000000000001",
    "checkpoint_after_actions": 2
  }]
}
```

Les chemins et l'UUID ci-dessus sont des exemples. Le lot doit utiliser le
schéma explicite v2. Les noms doivent correspondre exactement aux travaux du
lot et du workflow. Les plafonds sont recopiés du lot ; ce document ne permet
pas de les augmenter. Les identifiants, les trois fichiers, les outils, les
sources et les connexions restent vérifiés. Modifier cette sélection ne
reprend pas un historique dont les pins diffèrent.

## Installation et activation séparées

Le parcours est réservé à un paquet compatible 0.45 ou ultérieur, après revue
et installation de ce paquet. `continuation-plan` lit uniquement les trois
fichiers opérateur ; son résultat n'est pas une qualification d'exécution.

L'action `install-continuation` du gestionnaire requiert un paquet public
déjà installé et son reçu, ainsi que l'ancienne unité finie arrêtée et
désactivée. Elle ajoute un nouveau service et un nouveau timer désactivés,
puis recharge systemd. Elle ne lit aucun journal ou fichier de connexion
privé, ne crée pas d'état logique et ne lance aucun travail. Une installation
partielle est conservée pour examen, sans adoption automatique.

Les scripts standalone du manager et de l'entrée s'exécutent toujours avec
`-I -B`, après vérification des fichiers publics exacts, y compris
`pool_package.py`. Ne pas précréer le répertoire privé `continuations` : un état
partiel ou déjà présent ne devient pas une nouvelle préparation autorisée.

Le service de continuation garde les limites et le durcissement de l'unité
finie, avec `Restart=no`. Après configuration et lecture du plan, l'activation
humaine distincte `systemctl enable --now` du timer démarre les passages et les
active au boot. Le timer ne rattrape pas les déclenchements manqués. Chaque passage est borné ; les
checkpoints durables relient les passages successifs. Stop, les baux, les
capacités Runtime partagées et les droits du board continuent de s'appliquer.

Le résultat final doit provenir des tests de la génération finale. La
publication rassemble les modifications depuis la base publique d'origine,
puis une revue distincte doit viser cette publication exacte. Une preuve de
code terminé ou un passage du service réussi ne signifie ni projet terminé,
ni revue approuvée, ni PR fusionnée.

## Commentaires déplacés hors du roster natif (#51)

Les modules de `LINUX_EXECUTION_SOURCES` partagent un plafond de 2 Mio
(`family_sources.controller_sources`). Pour faire de la place à la
télémétrie bornée des tours (#51) sans toucher au plafond ni à la
composition du roster, ces commentaires ont été raccourcis dans le code ; le
code exécutable est inchangé (AST identique). Leur texte complet reste ici.

`continuation_pool_service.py` :

- `TARGET_FAILURE_CODES` : *Closed public error producers, frozen at
  development time. The provenance test scans public source; no source or
  owner file is read to classify a failure.*
- `_Pass.stop`, avant le compagnon d'arrêt : *Identical to the existing
  concrete stop companion: only metadata crosses threads. A fresh journal
  handle writes the normal immutable Stop markers; no Runtime is finished or
  claim released by inference.*
- `_target`, attente `not_before` : *Stop remains visible during a long
  provider backoff. Historical Core metadata creates neither a permit nor a
  Worker/client and cannot admit any execution.*
- `_target`, `paused_provider` inchangé : *No new cycle was admitted. Keep
  the failed source generation, plus this exact candidate for a fresh normal
  wake; never orphan a prepared_waiting intent.*
- `_target`, échec de la génération courante : *The own running generation
  becomes NeedsYou with actual counters. This creates no accepted checkpoint
  or cleanup. An already sealed code completion is not reopened merely
  because a later publication/handoff is uncertain.*
- `_LiveChecks`, lecteurs : *These readers take no broker/planner lock.
  Control can be called while the ordinary SDK holds them during a confirmed
  action. Their own source/test/permission guards continue to run
  unchanged.*
- `_Generation._pool`, réouverture : *Historical stopped preparation metadata
  stays stopped. It grants nothing; clean Core/checkpoint/retirement +
  Runtime proofs must still pass before a NEW queued generation.*

`dev_planner.py` :

- `_tool`, garde du backend : *Linux's concrete execution context applies
  the original backend authority at every boundary. Preserve its producer
  identity so a shared grant can prove both actions in one fresh SOURCE
  frame. Schema 1 keeps its existing composed backend guard.*
