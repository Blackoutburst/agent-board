# Blackoutburst — adaptateur Codex personnel

Ce module vérifie une connexion ChatGPT personnelle et fournit un exécutant local capable de conserver puis reprendre les sessions Codex. La première preuve locale du 15 octobre 2026 a réussi avec Codex `0.162.0-alpha.17.2` : connexion ChatGPT reconnue, réponse exacte reçue et tour terminé sans erreur.

Les commandes `doctor` et `probe` établissent une **preuve ponctuelle de connexion personnelle**. Les commandes [`runner`](RUNNER.md) ajoutent les tours de texte avec état durable, reprise exacte, déduplication et annulation. Les commandes [`context`](CONTEXT.md) lisent les sources d’un ticket via le MCP du board, les conservent localement et vérifient leur version avant un tour explicitement demandé. La commande [`worker`](WORKER.md) réserve un ticket choisi explicitement, exécute un tour textuel et publie sa réponse en revue, avec un journal des effets et une reprise après coupure. La commande [`events drain`](EVENTS.md) conserve les événements d’un ticket et peut soumettre un lot au worker sur demande explicite. La commande [`queue`](QUEUE.md) prévisualise les tickets éligibles et peut en sélectionner un seul dans un projet explicitement choisi. Aucun listener, orchestrateur général ou service cloud n’est activé par ce programme.

La source 0.32.0 compose le profil Linux explicite avec des
[espaces Git séparés par travail](GIT_WORKSPACES.md) et un
[worker de revue distinct de l’auteur](REVIEW-WORKER.md), un
[pool durable de travailleurs](WORKER-POOL.md), son
[pilote de développement](DEVELOPMENT-POOL.md) et son
[préparateur Linux](LINUX-POOL-PREPARATION.md).
Ces composants exigent les autorisations et les preuves locales de leur
contrôleur. Le [raccord de livraison](DEVELOPMENT_POOL_DELIVERY.md) relie un
résultat terminé aux [livraisons Git](GIT_DELIVERY.md), aux
[PR brouillon](GIT_PUBLICATION.md) et au passage en revue du ticket.
Un [installateur serveur séparé](../../deploy/worker_pool/README.md) prépare
un service arrêté pour un lot explicite de quatre résultats privés.
L’activation réelle et la hiérarchie manager/codeurs/reviewers/rédacteurs
restent à livrer ; cette version ne certifie ni une équipe active ni plusieurs
exécutions de modèles. Voir l’[état des travailleurs serveur](CLOUD-WORKERS.md).

Depuis 0.31.0, le raccord propriétaire [B/Host5](MESSAGE_APPEND_HOST5.md) permet
d’ajouter une cible de messages inactive depuis un slot personnel déjà
appairé et préparé. Il exige la source compatible et le catalogue C0 explicitement
remplacé, puis un grant humain distinct. Il ne lance ni service, ni modèle, ni
connexion fournisseur. L’équipe serveur hiérarchique et son attribution/exécution
parallèle de tâches restent à livrer séparément.

L’adaptateur 0.10 ajoute un [routage explicite des modèles](MODELS.md) et le [contrôle central de capacité](RUNTIME.md), tous deux sur option. Les préférences, observations et décisions de modèle restent liées au propriétaire et au binding. Le panneau **Agents → Runtime** montre les exécutions participantes, leur arrêt et leur capacité partagée ; il ne mesure pas les usages manuels de l’abonnement.

L’adaptateur 0.11 ajoute la commande [`usage`](USAGE.md). Elle lit les limites rapportées par le client Codex local sans appeler de modèle, puis peut transmettre une observation filtrée au panneau privé Runtime du propriétaire. Ces mesures datées restent distinctes des réservations d’exécution et expirent après deux minutes ; elles ne changent ni les droits, ni une pause, ni le modèle.

L’adaptateur 0.12 ajoute un [hôte local explicite](HOST.md) pour une liste fixée de tickets : configuration privée, activation séparée avec budget fini, Runtime obligatoire, reprises durables et arrêt au premier plan. Il est désactivé par défaut, sans sélection de nouveaux tickets ni service installé.

L’adaptateur 0.13 ajoute les [primitives du broker de développement](DEVELOPMENT.md), désactivées par défaut : fichiers explicitement autorisés, lectures bornées, remplacement exact avec contrôle du hash, budgets durables et rapprochement après coupure. Cette bibliothèque reste séparée des commandes existantes ; elle ne fournit ni transport de modèle, ni shell, ni backend réel de tests. Un stage dont la propriété ne peut pas être prouvée reste conservé et bloque la reprise jusqu’à inspection humaine.

L’adaptateur 0.14 ajoute un [backend natif de tests Python nommés](NATIVE_TESTS.md) et sa [supervision séparée](SUPERVISED_TESTS.md), sur option et après qualification locale. Le backend supervisé a été vérifié sur Mac avec des fichiers synthétiques, dont un crash du contrôleur suivi d’un nettoyage et d’un rapprochement sans réexécution. Linux reste non qualifié. Le parcours explicite de développement peut utiliser les tests qualifiés par leur nom ; aucune installation ne les active.

L’adaptateur 0.30 raccorde un [profil Linux explicite](LINUX_EXECUTION.md) au broker, au planificateur et aux familles. Son journal schema 3 neuf conserve les charges et intentions avant effet ; les préparations schema 2 restent non exécutables. Le backend et la policy doivent être qualifiés séparément sur les octets finaux et le même environnement. Les tests simulés de ce lot ne constituent pas une qualification Linux, et aucun service ou sélection automatique n’est ajouté.

Un [mode de réponse aux messages](MESSAGE_HOST.md) est disponible sur configuration explicite `schema_version: 2`, avec le flux confirmé et une portée Runtime de conversation. Le mode activité `schema_version: 1` reste distinct. Aucun listener permanent n’est installé ou activé.

L’adaptateur 0.18 ajoute le [parcours explicite de développement](DEVELOPMENT_CLI.md) : préparation en lecture seule, exécution finie avec droits et contexte frais, preuves datées, rapprochement des reçus et publication séparée en revue. Chaque opération démarre son propre dialogue fournisseur. Le nettoyage Runtime distingue les travaux historiques, les réponses aux messages et ce parcours ; aucun mode n’active les autres. Les tests de cette intégration utilisent uniquement des modèles et backends simulés.

L’adaptateur 0.21 sépare les scripts de validation des fichiers modifiables par le modèle. Le plan natif épingle leurs octets et leur identité ; le processus de test exécute les octets vérifiés, sans rouvrir un chemin mutable. Les anciens plans non épinglés sont refusés et doivent être préparés et qualifiés explicitement à nouveau. Les reçus antérieurs restent des observations datées, jamais une autorisation de relancer un ancien script.

L’adaptateur 0.24 conserve une connexion HTTP/TLS par lecteur MCP pour éviter les nouvelles connexions à chaque contrôle. Il conserve tous les RPC et barrières d’autorisation, sans cache d’autorité ni renvoi automatique après coupure. Les commandes et hôtes ferment explicitement leurs lecteurs et clients Runtime distincts. Le [contrat du transport persistant](PERSISTENT_MCP_TRANSPORT.md) présente les tests TLS locaux, les mesures datées et leurs limites.

L’adaptateur 0.24.1 [enregistre les refus avant lancement et regroupe les lectures familiales](ADMISSION_AND_READ_FRAMES.md). Un refus observé dans l’appel natif propriétaire peut produire une preuve explicite de non-lancement et terminer sa réservation comme bloquée avec nettoyage confirmé. Une coupure ou une preuve manquante reste en revue. Une vérification familiale partage ses observations parent/enfant dans une seule fenêtre fraîche ; les frontières suivantes relisent les sources. Les budgets ne changent pas et aucun ancien pilote n’est relancé ou requalifié par cette correction.

L’adaptateur 0.25 ajoute un [mode de lecture familiale MCP fixe](DEVELOPMENT_FRAMES.md), choisi et scellé explicitement, et des [fenêtres de supervision datées](SUPERVISED_TESTS.md). Le délai de 1,5 seconde est conservé et aucun heartbeat ne peut prolonger une autorisation expirée. Le déploiement du serveur, les mesures réelles, la nouvelle qualification native et le pilote restent à effectuer séparément ; les tests locaux et les anciens reçus ne les remplacent pas.

L’adaptateur 0.19 ajoute le [coordinateur familial](FAMILY.md) : un ticket parent choisi, deux agents temporaires du même propriétaire, trois espaces isolés et des modèles fixés explicitement. Après ouverture des tickets et qualification séparée des plans locaux, le contrôleur traite les deux enfants successivement, vérifie leurs fichiers/tests/nettoyages, puis fait vérifier leur intégration dans le parent. La publication du parent reste une commande distincte. Le verrou local de format 3 réserve les trois espaces atomiquement ; aucun service, sélection de ticket ou qualification automatique n’est activé. Les scénarios familiaux utilisent un vrai board temporaire et des modèles simulés ; ils ne constituent pas un pilote réel via l’abonnement.

## Utilisation

Python **3.10 ou supérieur**, sur macOS ou Linux/POSIX ; aucune bibliothèque Python externe n’est nécessaire à l’exécution. Codex doit être installé et connecté par son propriétaire au moyen de son interface ou de sa CLI officielle. L’authentification reste gérée par Codex. [Documentation officielle de connexion](https://learn.chatgpt.com/docs/auth)

Depuis ce dossier, vérifier l’installation sans appeler de modèle :

```sh
python3 -m blackoutburst_agents doctor --codex "/chemin/absolu/codex" --output docs/doctor-local.json
```

Puis demander explicitement un unique tour de test, qui consomme une petite part du quota de l’abonnement :

```sh
python3 -m blackoutburst_agents probe --codex "/chemin/absolu/codex" --timeout 120 --output docs/probe-local.json
```

`--codex` est facultatif si l’exécutable est dans `PATH`. `probe` accepte un `--model` choisi explicitement parmi les modèles accessibles au compte ; sinon il utilise le défaut du client. Aucun modèle de remplacement n’est choisi automatiquement. Le code de sortie vaut `0` pour une vérification réussie et `2` si elle est bloquée. Les reçus JSON sont expurgés et écrits avec des permissions privées. [Référence officielle de la CLI](https://learn.chatgpt.com/docs/cli/reference)

Le probe utilise un dossier temporaire vide, une session éphémère et le sandbox en lecture seule. Les fonctionnalités d’exécution identifiées sont désactivées ; la réponse demandée est uniquement un nonce aléatoire. Un événement d’outil inattendu, un échec tardif, une sortie incohérente, un dépassement ou un nettoyage incomplet empêchent de déclarer la preuve réussie. Le parseur constate ces anomalies ; il ne constitue pas à lui seul une barrière d’exécution. [Exécution non interactive officielle](https://learn.chatgpt.com/docs/non-interactive-mode)

## Authentification et portée

Le programme ne lit aucun fichier d’authentification Codex et ne lance aucune connexion Codex. La lecture du board réutilise uniquement son fichier de connexion MCP privé, créé par le connecteur commun ; elle ne copie pas son jeton dans le contexte. Il conserve `HOME` et `CODEX_HOME`, filtre les variables de fournisseurs et refuse le mode clé API. **Aucun repli vers une facturation API n’est prévu.** Chaque propriétaire doit gérer sa propre connexion officielle ; ne pas partager les identifiants ni copier les fichiers d’authentification ou les caches d’un compte vers un autre exécutant.

La preuve ne valide pas encore le compte d’un second propriétaire, l’éligibilité à un hébergement mutualisé, les quotas futurs ou le renouvellement de connexion. La reprise possède ses propres tests dans l’exécutant local. L’option `--ignore-user-config` utilisée pour ce test n’établit pas l’absence de toutes les politiques système ou administrées. Un succès local ne certifie donc pas tous les environnements possibles.

## Intégration au board

Ce module complète les intégrations de fournisseurs du board. La création et la personnalisation des agents, le choix des fournisseurs, les déclencheurs et l’orchestration restent du côté du board. Cet adaptateur se concentre sur l’exécution Codex personnelle et ses preuves de fonctionnement.

Le raccordement en lecture utilise les identités persistantes du board. Le contexte vérifie les relations propriétaire, agent, projet, ticket et conversation, puis la fraîcheur des sources. Le board fournit aussi des réservations, une file de travail, des délégations et des demandes humaines ; `worker` raccorde une réservation explicite et une publication de texte. `worker run --human-owner` et `events drain --human-owner` autorisent une question au participant choisi puis une reprise explicite dans la même session après sa réponse. Le [pilote réel](docs/local-event-human-2026-10-15.json) a confirmé deux tours dans la même session, une seule publication et aucune nouvelle inférence à la relance. Avec le board 2.3 et l’adaptateur 0.9, `queue run --execute` raccorde la sélection d’un seul ticket. Le développement local explicite et sa délégation familiale bornée sont décrits dans [DEVELOPMENT_CLI.md](DEVELOPMENT_CLI.md) et [FAMILY.md](FAMILY.md). L’exécution distante entre plusieurs ordinateurs reste hors de ce parcours. Les libellés d’un work local ne constituent pas des autorisations.

L’[interface du module et la répartition proposée](INTEGRATION.md) permettent de le raccorder à un fournisseur du board sans créer un second système de profils.

## Vérification

```sh
python3 -m pytest tests -q
```

Les dépendances de test sont celles de `requirements-dev.txt` à la racine du dépôt ; `pytest` inclut les scénarios adversariaux du host. L’exécution de l’adaptateur ne nécessite aucune de ces dépendances de test.

Les tests utilisent des exécutables factices, des transports simulés et le vrai Board dans une base temporaire, sans inférence : authentification, arguments et environnement, flux JSON, limites, nettoyage, état durable, accès concurrents, doublons, reprise, annulation, sources MCP, bornes Unicode, versions, isolation et refus d’un contexte périmé. Le test réel de reprise a enchaîné deux tours dans des processus distincts : le second a retrouvé une information du premier. Une nouvelle tentative identique a ensuite récupéré la réponse enregistrée sans nouveau tour.

- [Reçu du diagnostic local](docs/local-doctor-2026-10-15.json)
- [Reçu du tour réel](docs/local-probe-2026-10-15.json)
- [Reçu de reprise réelle et de déduplication](docs/local-runner-resume-2026-10-15.json)
- [Reçu de lecture du contexte sur le MCP réel](docs/local-context-2026-10-15.json)
- [Reçu du tour Codex avec l’extrait approuvé](docs/local-context-model-2026-10-15.json)
- [Reçu du ticket réel traité puis rejoué sans second tour](docs/local-worker-2026-10-15.json)
- [Reçu de lecture réelle des événements sans modèle](docs/local-event-drain-2026-10-15.json)
- [Revue du parcours question/réponse et limites de validation](docs/review-human-2026-10-15.md)
- [Reçu du pilote réel de routage et capacité partagée](docs/local-managed-routing-2026-10-15.json)

Le contrôle MCP a vérifié la lecture réelle, la conservation locale et la relecture stable d’un contexte de 9 400 octets. Après autorisation explicite du propriétaire, un contrôle distinct a transmis l’extrait inspecté de 10 686 octets à un unique tour Codex via son abonnement ChatGPT : le titre du ticket a été restitué exactement, sans outil ni modification du dossier de travail. Ce second contrôle utilise la photographie approuvée ; la fraîcheur MCP est vérifiée par le premier contrôle. Il ne démontre pas une mémoire sémantique ni un travail autonome.

Le pilote `worker` sur le MCP 2.0.1 a réservé un ticket choisi, transmis son contexte de 8 905 octets, reçu le titre exact via la connexion ChatGPT et publié une seule réponse en revue. Une invocation identique a retrouvé le résultat sans nouveau tour ni changement de version du ticket. Le dossier est resté vide et le nettoyage a été confirmé.

Le pilote géré #19 sur le board 2.4 a choisi le modèle par la politique du propriétaire, terminé un unique tour avec réservation centrale et publié une seule réponse. Le rejeu a conservé la décision initiale et retrouvé le résultat sans nouvelle réservation ni inférence. Le nettoyage du processus et le dossier vide ont été vérifiés.

Ces reçus ne contiennent ni jeton de connexion, ni transcription, ni sortie brute du client.

La version 0.7 lit les préférences du profil du board 2.1 et vérifie leur version avant composition du contexte. La langue, le style et la spécialisation peuvent guider la réponse dans le périmètre demandé ; fournisseur/modèle restent des préférences et ne changent pas automatiquement l’exécutant. Les droits, connexions et tâches hors portée ne sont pas transmis au modèle.

La version 0.8 raccorde les questions au journal événements : la réponse reprend le lot et la session d’origine, et son événement est dédupliqué même après coupure ou abandon. Le rapprochement optionnel d’une question dont l’accusé a été perdu reste en lecture seule. Les scénarios utilisent un vrai Board temporaire et des modèles simulés ; la question/réponse avec un humain réel reste une preuve distincte.

La [mémoire explicite de décisions](MEMORY.md) permet d’épingler une ancienne source humaine du ticket, de la relire exactement avant les tours, puis de consulter/corriger/réinitialiser ses résumés localement. Elle reste optionnelle, bornée et séparée des profils, caches et sessions fournisseur.
La [politique optionnelle du planificateur](PLANNER_POLICY.md) fournit un dossier vide distinct du checkout et une politique de commandes en lecture seule, après qualification locale explicite sur Mac. Le transport habituel reste inchangé ; cette bibliothèque n’active ni modèle, ni développement, ni listener automatiquement.
