# Lire les événements d’un ticket

`events drain` lit une fois le flux d’activité d’un ticket explicitement choisi, conserve les événements dans un journal privé, puis quitte. Par défaut, il ne lance aucun modèle. L’option `--execute` peut traiter un lot éligible avec le [worker textuel](WORKER.md), puis publier son résultat en revue.

Ce premier parcours ne démarre ni listener permanent, ni sélection automatique de tickets. Il ne réactive aucune ancienne automatisation. L’instruction est fournie localement par l’opérateur ; le texte d’un message reste du contexte, sans autoriser de nouvelles actions.

## Prévisualiser une lecture

Le board doit exposer `list_activity(order="asc")`. Depuis la version **2.0.2**, les nouveaux messages des conversations liées aux tickets produisent aussi un événement `ticket_message_posted`. Les messages antérieurs à cette version ne sont pas ajoutés rétroactivement au flux. Ils peuvent encore figurer dans les sources de conversation lues par le worker, selon ses limites.

```sh
python3 -m blackoutburst_agents events \
  --journal /chemin/prive/evenements \
  drain \
  --connection-file /chemin/prive/connexion-board.json \
  --session session-stable-de-ce-travail \
  --owner identifiant-proprietaire --agent identifiant-agent \
  --project identifiant-projet --ticket identifiant-ticket \
  --instruction-file /chemin/prive/instruction.txt \
  --workspace /chemin/dossier-de-travail --codex /chemin/absolu/codex \
  --after 0 --limit 50 --max-pages 4
```

Le journal crée un suivi privé dont la portée et la configuration restent fixes : propriétaire, identité d’agent, projet, ticket, URL, session MCP, instruction, dossier, exécutable, modèle et délai. `--after` fixe la position initiale ; les passages suivants reprennent la position conservée. Relancer avec la même configuration et la même valeur initiale. Les limites de pagination peuvent changer.

Une page et son nouveau curseur sont enregistrés dans la même transaction. Lire un événement ne signifie pas que son travail est terminé. Les événements humains pertinents sont regroupés dans un lot durable, distinct de ce curseur. Les messages et mises à jour émis par des agents ne déclenchent pas de modèle, afin d’éviter les boucles.

Les événements déclencheurs de cette tranche sont `ticket_created`, `ticket_updated`, `ticket_message_posted` et `human_input_resolved`, lorsqu’ils sont attribués à un humain. Les conversations sans ticket, mentions ciblant un agent, changements de profil et réveils distants ne sont pas couverts.

## Exécuter un lot explicitement

Ajouter les deux états privés du worker avant `drain`, puis `--execute` aux options :

```sh
python3 -m blackoutburst_agents events \
  --journal /chemin/prive/evenements \
  --work-journal /chemin/prive/journal-board \
  --state /chemin/prive/sessions-codex \
  drain [les mêmes options de portée et de configuration] --execute
```

La commande ne choisit aucun autre ticket. Elle exige un événement éligible, les permissions actuelles et un ticket prêt avec une réservation compatible. Un message n’ouvre pas automatiquement un ticket `review` ou `blocked`. Le programme conserve le lot en attente jusqu’à ce que l’état du ticket permette un départ autorisé.

Chaque lot possède une clé de requête stable. L’intention de départ est enregistrée avant l’appel au worker. Si le worker a publié puis que le programme s’est arrêté avant de confirmer le lot, la tentative suivante réconcilie cette même opération, même si le ticket est déjà passé en revue. Elle ne relance pas le modèle pour ce lot. Les événements survenus pendant le travail restent disponibles pour un passage ultérieur.

Une invocation lit au plus 20 pages de 200 événements, traite au plus un lot, puis termine. Si la dernière page autorisée est pleine, `more_available` demande un nouveau passage avant tout départ du modèle. Un lot regroupe au plus 200 événements déclencheurs ; le journal refuse plus de 10 000 événements déclencheurs en attente. Les événements conservés contiennent des métadonnées, sans corps de message, instruction distante ou jeton.

Lorsque cette capacité est atteinte, la page refusée ne fait pas avancer le curseur. Avec `--execute`, le programme peut traiter un lot déjà conservé pour libérer de la place, même si le reste du flux n’est pas encore lu. Les sources et permissions sont toujours revérifiées par le worker. En prévisualisation, `event_buffer_full` indique cette attente ; les événements non enregistrés seront relus au prochain passage.

## Lire l’état et résoudre un lot

Le reçu indique un état, un code, la position du curseur, les compteurs et les identifiants locaux. Les états `idle`, `pending`, `more_available` et `waiting` ne déclarent pas le ticket terminé. `complete` confirme seulement la publication du worker ou la résolution du lot.

```sh
python3 -m blackoutburst_agents events \
  --journal /chemin/prive/evenements \
  status --subscription identifiant-du-suivi
```

Un lot `needs_review` ne déclenche pas de nouveau travail. Examiner d’abord l’opération dans le journal du worker. Si elle doit être abandonnée, suivre le parcours explicite du guide worker, notamment la vérification des effets sur le board. Puis réconcilier le lot avec cette opération locale achevée ou abandonnée :

```sh
python3 -m blackoutburst_agents events \
  --journal /chemin/prive/evenements \
  --work-journal /chemin/prive/journal-board \
  reconcile --batch identifiant-lot --operation identifiant-operation-worker
```

Cette résolution ne contacte pas le board et ne lance aucun modèle. Elle vérifie la portée, la configuration et la clé de requête exactes. Une opération encore incertaine ou appartenant à un autre lot est refusée. Une résolution par abandon conserve cette décision et ne prétend pas que le ticket a été accompli.

Les tests couvrent la pagination, les doublons, les reprises après ingestion ou publication, la concurrence, la révocation, les modifications humaines et l’absence de boucle entre agents. La validation d’un service permanent et de plusieurs comptes réels reste distincte de cette commande ponctuelle.
