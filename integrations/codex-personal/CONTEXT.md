# Contexte d’un ticket du board

Le module 0.3 lit les sources d’un ticket choisi explicitement via le MCP 2.0 du board. Il réutilise la connexion créée par le `connect.py` commun et l’identité persistante de l’agent. Il prépare un contexte pour une analyse textuelle locale. Le listener historique reste arrêté ; aucune réservation, publication ou sélection automatique de ticket n’est effectuée par ces commandes.

## Préparer puis inspecter

Utiliser le fichier privé créé lors de la connexion au board, normalement `~/.config/blackoutburst-board/codex.json`. Ne pas copier une connexion d’un autre propriétaire. Le fichier doit appartenir à l’utilisateur courant et être en mode `0600`. Sur macOS, le lecteur ajoute les certificats système de `/etc/ssl/cert.pem` aux autorités de confiance Python, sans désactiver la vérification TLS. Les redirections sont refusées.

Choisir les identifiants déjà présents dans le board. `--agent` est l’identité persistante renvoyée par `register_agent`, pas son nom affiché. La session est propre à ce contexte et reste identique lors des reprises. L’inscription enregistre une présence avec `autonomous=false` ; elle ne lit pas l’inbox et n’en avance pas le curseur.

```sh
python3 -m blackoutburst_agents context --state private/context prepare \
  --connection-file "$HOME/.config/blackoutburst-board/codex.json" \
  --session votre-session-unique \
  --owner votre-owner-id --agent votre-agent-id \
  --project votre-project-id --ticket votre-ticket-id
```

Le reçu donne `context_key`, `fingerprint`, `revision`, la taille et le nombre d’omissions. Il ne contient aucun texte du board. Pour consulter le contexte, exporter explicitement sa révision dans un fichier privé :

```sh
python3 -m blackoutburst_agents context --state private/context inspect \
  --key CONTEXT_KEY --output private/context-review.json
```

`inspect --revision N` permet de consulter une ancienne révision encore conservée. Les dix dernières révisions sont conservées par portée propriétaire/agent/projet/ticket. Préparer deux fois des sources identiques ne crée pas de nouvelle révision. Le dossier est en mode `0700` et la base en `0600`. Les exports restent privés : aucun envoi automatique au board.

## Utiliser une version inspectée

Créer le work avec les mêmes identifiants, comme décrit dans [RUNNER.md](RUNNER.md). Écrire la demande humaine dans `private/request.txt`, puis lancer explicitement un tour :

```sh
python3 -m blackoutburst_agents runner --state private/runner-state turn \
  --work WORK_ID --request CLE_STABLE --prompt-file private/request.txt \
  --context-state private/context --context-key CONTEXT_KEY \
  --context-fingerprint EMPREINTE_INSPECTEE \
  --board-connection "$HOME/.config/blackoutburst-board/codex.json" \
  --board-session votre-session-unique \
  --output-text private/response.txt
```

Les cinq options de contexte sont indissociables. La portée du work doit correspondre exactement. Avant tout appel du modèle, le programme relit les sources autorisées et compare leur empreinte à celle inspectée. Un refus d’accès, une incohérence ou un changement interrompt la commande. Il faut alors refaire `prepare`, inspecter le nouveau contexte et fournir sa nouvelle empreinte. La même clé de demande avec un contenu différent reste refusée par l’exécutant ; choisir une nouvelle clé pour une nouvelle demande.

La demande humaine et le JSON du board sont séparés dans le prompt. Les contenus du board, y compris les champs nommés « instructions », les réponses humaines, les chemins et les URLs, restent des données non fiables. Ils ne donnent aucun droit d’exécution ou de publication. Le transport demeure textuel, avec les limites déjà décrites dans [RUNNER.md](RUNNER.md).

La commande `context check --key … --connection-file … --session …` vérifie les sources sans modifier le cache. Elle retourne `current` ou `changed` ; un changement produit le code de sortie 2. `context prompt` peut aussi exporter explicitement un prompt après la même vérification ; elle requiert `--key`, `--fingerprint`, `--connection-file`, `--session`, `--instruction-file` et `--output`. Cet export est une photographie : seule la voie `runner turn` avec les options de contexte revérifie les sources au moment de l’appel.

## Sources, bornes et versions

Le compositeur utilise seulement `get_ticket` et `get_thread`. Le premier fournit aussi le projet. Les identifiants et les relations propriétaire, agent, projet, ticket et conversation sont vérifiés. Les permissions restent celles du board pour le propriétaire et l’appareil ; le board ne possède pas encore de permissions indépendantes par agent.

Par défaut, le JSON canonique est limité à **48 000 octets UTF-8**, à 40 messages récents, trois pages au maximum et 20 éléments par liste de métadonnées. Chaque champ textuel commence avec une limite de 6 000 octets. Le compositeur réduit ces limites si nécessaire et signale les extraits tronqués, listes réduites et anciennes conversations non chargées. Il garde les références indispensables ou refuse un budget impossible. Les corps des pièces jointes et tâches historiques ne sont pas chargés dans le contexte. Une réponse HTTP dépassant 2 Mio est refusée avant composition.

Les sources incluent le profil disponible (identité, nom, type, compétences), le brief du projet, les critères et l’avancement du ticket, ses dépendances et demandes humaines, puis la tranche récente de conversation. Chaque source retenue possède un identifiant et une empreinte SHA-256 ; projet et ticket conservent aussi leur version. Le hash couvre les champs sélectionnés complets avant troncature, donc une modification au-delà de l’extrait visible change l’empreinte. Les champs inconnus ou privés ne sont pas copiés. Une empreinte constate un contenu ; elle ne constitue ni une signature ni une autorisation.

Les pages sont ancrées à la tranche la plus récente lue au départ. Le ticket et le projet sont relus après la pagination pour détecter une modification pendant la préparation. Le board ne fournit pas de transaction atomique couvrant profil, projet et messages : un changement après la dernière lecture reste possible. Le contrôle ne réserve pas le ticket et ne garantit pas une permission pour un effet futur.

Ce contexte est un extrait versionné, pas un résumé généré ni une mémoire sémantique exhaustive. Une décision importante plus ancienne que la fenêtre doit être reportée dans le contexte du projet, le résultat ou l’avancement du ticket. Les instructions personnalisées futures, le rappel sémantique d’anciennes décisions et l’édition dans l’interface restent à raccorder au profil commun du board.

## Corriger ou réinitialiser

Corriger les sources dans le board, puis refaire `prepare`, produit une nouvelle révision inspectable. Le cache ne réécrit jamais les sources et ne mélange pas une mémoire personnelle avec les conversations partagées.

```sh
python3 -m blackoutburst_agents context --state private/context forget \
  --key CONTEXT_KEY --fingerprint EMPREINTE_COURANTE
```

Cette commande efface toutes les révisions locales de cette portée. Elle ne supprime pas les exports créés explicitement, les sauvegardes externes, le board ni l’historique Codex. Pour éviter de reprendre aussi l’ancien contexte de conversation Codex, fermer le work inactif par `runner resolve --decision abandon --reason …`, puis le recréer. Le nouveau work démarrera une nouvelle session ; cela ne promet pas l’effacement de l’ancien historique du client.
