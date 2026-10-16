# Hôte local explicite

Le mode activité `schema_version: 1` décrit ici surveille uniquement les tickets choisis dans une configuration locale privée. Il collecte leur activité avec `EventDrainer`, puis utilise le worker textuel existant et le contrôle Runtime pour un lot éligible. Il ne choisit aucun nouveau ticket et n’installe aucun service. Le mode de réponse `schema_version: 2` suit séparément les [messages humains confirmés](MESSAGE_HOST.md), y compris les conversations générales ; aucun mode n’est activé par défaut.

La création est désactivée par défaut. `enable` autorise un budget fini de nouveaux lots ; `run` est ensuite une commande distincte, au premier plan. Rien n’a été activé par l’installation de cette version. Le propriétaire doit lancer et conserver le processus sur son hôte personnel. Le fonctionnement sur un serveur permanent ou Mac éteint n’est pas démontré par les tests locaux.

## Configuration privée

Créer un fichier JSON en mode `0600`, avec des chemins absolus existants. Il ne contient aucun token ni mot de passe : `connection_file` désigne le fichier privé d’une connexion déjà appariée. Les identifiants, tickets et version de profil sont choisis explicitement par le propriétaire.

```json
{
  "schema_version": 1,
  "owner_id": "OWNER_ID",
  "board_endpoint": "https://board.example/mcp",
  "interval_seconds": 30,
  "max_pages": 2,
  "page_limit": 50,
  "dispatch_budget": 2,
  "targets": [
    {
      "id": "ticket-principal",
      "agent_id": "AGENT_ID",
      "project_id": "PROJECT_ID",
      "ticket_id": "TICKET_ID",
      "board_session_id": "SESSION_ID",
      "profile_version": 1,
      "connection_file": "/absolute/private/connection.json",
      "instruction": "Préparer une réponse textuelle pour ce ticket et la soumettre en revue.",
      "workspace": "/absolute/workspace",
      "executable": "/absolute/path/to/codex",
      "model": "MODEL_EXPLICITLY_AVAILABLE_TO_THIS_OWNER",
      "timeout": 120,
      "after": 0
    }
  ]
}
```

`human_owner_id` peut fixer le destinataire d’une question par lot. Une réponse reprend la même opération et la même session ; elle ne consomme pas un deuxième lot du budget. Le worker permet au plus une question et sa reprise. `after` est la frontière initiale immuable de la souscription : zéro inclut l’activité historique, qui peut rendre le premier lot immédiatement disponible après activation.

Une politique locale de modèles est également possible via le champ `routing`, au format produit par `routing_configuration` de l’adaptateur : `{policy, policy_hash, task_class}`. Voir [MODELS.md](MODELS.md). Le modèle ou cette politique est obligatoire ; aucun fournisseur ou compte de secours n’est choisi. La décision existante reste épinglée lors d’une reprise.

Bornes : 1–16 cibles, 1–20 nouveaux lots par activation, 5–300 secondes entre passages, 1–4 pages de 1–200 événements par cible et par drain, 1–300 secondes par tour modèle. Un tick ne lance qu’un seul lot. Un propriétaire, un serveur et une configuration sont fixés par journal. Les cibles d’un profil utilisent la même session, la même connexion et la même version.

```sh
blackoutburst-agents host --journal /absolute/private/host configure --config /absolute/private/host.json
blackoutburst-agents host --journal /absolute/private/host status
blackoutburst-agents host --journal /absolute/private/host enable --revision 1
blackoutburst-agents host --journal /absolute/private/host tick
blackoutburst-agents host --journal /absolute/private/host run
```

Utiliser la révision courante renvoyée par `status`, pas systématiquement `1`. `enable` ne démarre pas de processus. Une réactivation explicite renouvelle le budget configuré ; un redémarrage du processus ne le renouvelle jamais. Une tentative répétée d’activation avec une ancienne révision est refusée.

La configuration normalisée est copiée dans le journal privé avec ses empreintes d’instruction, de politique et de connexion. Modifier le fichier JSON d’origine ne change pas l’hôte. `configure` est idempotent seulement pour la même configuration. Le profil est vérifié avant les départs et son contenu, sa politique et son appareil sont également épinglés dès la première lecture. Changement de profil, de connexion ou de droits : arrêt et contrôle humain, sans migration automatique vers un nouveau compte/modèle.

## Arrêt et reprise

```sh
blackoutburst-agents host --journal /absolute/private/host stop
```

`stop` écrit la désactivation dans le journal, même pendant qu’un autre processus possède le verrou d’exécution. Le processus actif la vérifie dans l’attente, avant les admissions et via le callback d’annulation du runner. SIGINT/SIGTERM demandent le même arrêt. L’arrêt demandé ne prouve pas l’arrêt d’un processus modèle : le runner et Runtime conservent les preuves de nettoyage et une incertitude reste pour revue.

Le verrou canonique se trouve sous `.local/state/blackoutburst-agents/hosts` dans le domicile du compte OS (résolu par `getpwuid`, indépendant de la variable `HOME`), sans option de redirection en CLI. Il est commun aux différentes valeurs de `--journal` pour le même propriétaire et la même origine normalisée. Il épingle aussi le chemin et l’UUID du journal : un dossier remplacé au même chemin ne devient pas une nouvelle autorisation. Le verrou couvre uniquement ce compte OS sur cette machine. Il ne remplace pas une location distribuée entre plusieurs hôtes.

Chaque nouveau batch débite son budget et obtient une intention durable. L’UUID exact du futur enfant `WorkJournal` est préalloué dans cette même transaction, avant même la création de l’enfant. `WorkJournal.begin` crée uniquement cet ID ; une request préexistante portant un autre ID est refusée, même vide. Cette preuve reste valable si le stockage devient indisponible pendant l’enregistrement d’un refus. Après une coupure, l’hôte reprend ce batch, sa clé et cet enfant exacts. Il n’adopte pas une opération manuelle indépendante. Une réponse modèle ou publication déjà enregistrée est rapprochée ; elle n’est pas réexécutée. Les journaux expérimentaux antérieurs qui contiennent une intention sans UUID enfant sont refusés comme corrompus ; aucune adoption ni migration automatique de ces intentions n’est effectuée. Cette première version distribuée crée toujours l’UUID dans l’intention.

Si un batch attend une réponse humaine ou un rapprochement, ce MVP garde une seule opération ouverte et diffère les autres cibles. Le serveur sait désormais libérer une question et sélectionner un autre ticket ; cet hôte ne demande pas ce parallélisme. Les événements d’agents, dont `ticket_released`, ne créent aucun nouveau lot modèle. Les événements humains autorisés restent les déclencheurs de l’EventJournal.

Le journal expose `needs_review` si un effet ou un arrêt demande un examen. Utiliser d’abord les commandes de récupération existantes dans [WORKER.md](WORKER.md), [EVENTS.md](EVENTS.md) et [RUNTIME.md](RUNTIME.md), avec les sous-dossiers `work`, `events`, `runtime` et `runs` de l’hôte. Ne pas supprimer ses journaux ou son verrou pour forcer un nouveau départ.

Une fois le batch EventJournal complet et l’enfant exact terminé ou abandonné explicitement, rapprocher le reçu de l’hôte hors connexion :

```sh
blackoutburst-agents host --journal /absolute/private/host stop
blackoutburst-agents host --journal /absolute/private/host reconcile --batch BATCH_ID
blackoutburst-agents host --journal /absolute/private/host status
```

Cette commande n’abandonne aucun travail, n’acquitte aucune réservation distante et ne réactive rien. Un enfant inconnu/non lié ou non terminal est refusé ; une incohérence de stockage conserve l’arrêt. La reconfiguration de cet hôte et le déplacement vers un autre ordinateur exigent une migration contrôlée des états ; aucune commande de migration automatique n’est fournie dans cette version.

## Vérifications et limites

Les tests utilisent des journaux SQLite et le Board temporaires, des transports de modèle simulés et aucune connexion réseau. Ils couvrent notamment : 1 000 ticks vides sans claim/modèle/publication, budget et arrêt durables, verrou propriétaire, alias d’URL, journal remplacé, enfants indépendants, coupures entre préparation/liaison/publication, quotas Runtime, changement de profil et question/réponse sans troisième tour.

Le host reste un worker de texte. Il n’exécute pas de développement de code, ne délègue pas vers d’autres modèles et ne réveille pas une conversation de l’application Codex. Les mentions hors tickets suivis relèvent du [mode réponses explicite](MESSAGE_HOST.md) ; `read_inbox` n’est utilisé par aucun des deux modes. Les mesures d’usage sont informatives : une remise à zéro ou un pourcentage disponible ne réactive jamais l’hôte. Les droits et l’admission Runtime sont vérifiés par leurs contrôles existants.
