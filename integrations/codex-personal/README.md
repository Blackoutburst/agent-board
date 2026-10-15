# Blackoutburst — adaptateur Codex personnel

Ce module vérifie une connexion ChatGPT personnelle et fournit un exécutant local capable de conserver puis reprendre les sessions Codex. La première preuve locale du 15 octobre 2026 a réussi avec Codex `0.162.0-alpha.17.2` : connexion ChatGPT reconnue, réponse exacte reçue et tour terminé sans erreur.

Les commandes `doctor` et `probe` établissent une **preuve ponctuelle de connexion personnelle**. Les commandes [`runner`](RUNNER.md) ajoutent les tours de texte avec état durable, reprise exacte, déduplication et annulation. Aucun listener, orchestrateur général ou service cloud n’est activé par ce programme.

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

Le programme ne lit aucun fichier d’authentification et ne lance aucune connexion. Il conserve `HOME` et `CODEX_HOME`, filtre les variables de fournisseurs et refuse le mode clé API. **Aucun repli vers une facturation API n’est prévu.** Chaque propriétaire doit gérer sa propre connexion officielle ; ne pas partager les identifiants ni copier les fichiers d’authentification ou les caches d’un compte vers un autre exécutant.

La preuve ne valide pas encore le compte d’un second propriétaire, l’éligibilité à un hébergement mutualisé, les quotas futurs ou le renouvellement de connexion. La reprise possède ses propres tests dans l’exécutant local. L’option `--ignore-user-config` utilisée pour ce test n’établit pas l’absence de toutes les politiques système ou administrées. Un succès local ne certifie donc pas tous les environnements possibles.

## Intégration au board

Ce module complète les intégrations de fournisseurs du board. La création et la personnalisation des agents, le choix des fournisseurs, les déclencheurs et l’orchestration restent du côté du board. Cet adaptateur se concentre sur l’exécution Codex personnelle et ses preuves de fonctionnement.

Le raccordement futur doit préciser l’identité de l’agent, les permissions, les événements et la réservation des tickets. L’exécutant gère déjà ses contextes locaux ; leurs libellés ne constituent pas des autorisations du board. Ce module ne fournit aucun endpoint de board ni protocole d’orchestration déployé.

L’[interface du module et la répartition proposée](INTEGRATION.md) permettent de le raccorder à un fournisseur du board sans créer un second système de profils.

## Vérification

```sh
python3 -m unittest discover -s tests -v
```

Les **79 tests** utilisent des exécutables factices, sans inférence : authentification, arguments et environnement, flux JSON, limites, nettoyage, état durable, accès concurrents, doublons, reprise et annulation. Le test réel de reprise a enchaîné deux tours dans des processus distincts : le second a retrouvé une information du premier. Une nouvelle tentative identique a ensuite récupéré la réponse enregistrée sans nouveau tour.

- [Reçu du diagnostic local](docs/local-doctor-2026-10-15.json)
- [Reçu du tour réel](docs/local-probe-2026-10-15.json)
- [Reçu de reprise réelle et de déduplication](docs/local-runner-resume-2026-10-15.json)

Ces reçus ne contiennent ni jeton de connexion, ni transcription, ni sortie brute du client.
