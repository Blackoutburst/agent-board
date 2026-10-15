# Lire les événements d’un ticket

`events drain` lit une fois le flux d’activité d’un ticket explicitement choisi, conserve les événements dans un journal privé, puis quitte. Par défaut, il ne lance aucun modèle. L’option `--execute` peut traiter un lot éligible avec le [worker textuel](WORKER.md), puis publier son résultat en revue.

Ce parcours ne démarre ni listener permanent, ni sélection automatique de tickets. Il ne réactive aucune ancienne automatisation. L’instruction est fournie localement par l’opérateur ; le texte d’un message reste du contexte, sans autoriser de nouvelles actions.

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

Le journal crée un suivi privé dont la portée et la configuration restent fixes : propriétaire, identité d’agent, projet, ticket, URL, session MCP, instruction, dossier, exécutable, modèle, délai et éventuel destinataire humain. `--after` fixe la position initiale ; les passages suivants reprennent la position conservée. Relancer avec la même configuration et la même valeur initiale. Les limites de pagination peuvent changer.

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

## Poser une question puis reprendre

À partir de la version 0.8, ajouter `--human-owner identifiant-proprietaire` dès le premier aperçu autorise une question d’information au participant choisi. Le board doit être en version 2.0.3 ou supérieure. L’option reste fixe pour ce suivi, y compris lors des aperçus suivants ; l’ajouter, la retirer ou changer le destinataire après création du suivi est refusé. Le destinataire ne peut pas être choisi par le modèle. La commande ne demande ni secret, ni approbation, ni action externe.

Exécuter ensuite exactement la même commande avec `--execute`. Si une information manque, le worker publie une seule question dans **Needs you**, libère sa réservation et conserve l’opération et le lot. Le reçu `pending` / `worker_pending` peut indiquer cette attente. Utiliser son `worker_operation_id` pour consulter l’étape :

```sh
python3 -m blackoutburst_agents worker \
  --journal /chemin/prive/journal-board \
  status --operation identifiant-operation-worker
```

L’étape `waiting_human` attend la réponse dans le board. Après cette réponse, relancer la même commande `events drain` avec les mêmes chemins, session, instruction, profil, destinataire et `--execute`. La reprise utilise la même session Codex et un second tour distinct. Un passage avant réponse ne lance pas de nouveau tour. Aucun autre ticket ni ancienne opération manuelle n’est récupéré automatiquement ; la clé du lot est créée par le journal, sans option `--request` à inventer.

La réponse est rattachée durablement à sa question et à son lot d’origine. Son événement ne déclenche pas un troisième tour, même s’il arrive après la publication ou l’abandon explicite du lot. Les autres commentaires et modifications restent disponibles pour un passage ultérieur. Le worker relit toujours la réponse et les droits sur le board ; l’événement n’accorde aucune autorisation.

Conserver la version du profil pendant l’attente. Un changement de profil, de destinataire, une réaffectation ou une révocation interdit la reprise et impose une revue. Une attente humaine normale ne doit pas être clôturée avec `events reconcile` : cette commande sert seulement à confirmer une opération déjà achevée ou explicitement abandonnée après inspection.

Le journal événements migre transactionnellement de v1 à v2 dès son ouverture, y compris pour `status` ou un aperçu. Il conserve les positions, événements et lots existants. Sauvegarder le dossier privé avant cette mise à niveau ; un ancien adaptateur refuse le journal v2. Aucun texte de question/réponse n’est ajouté au journal événements : il conserve seulement les identifiants de liaison et une empreinte de question. Les textes restent dans le journal privé du worker et sur le board autorisé.

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

Par défaut, cette résolution reste locale et ne lance aucun modèle. Elle vérifie la portée, la configuration et la clé de requête exactes. Une opération encore incertaine ou appartenant à un autre lot est refusée. Une résolution par abandon conserve cette décision et ne prétend pas que le ticket a été accompli.

Les tests couvrent la pagination, les doublons, les reprises après ingestion ou publication, la concurrence, la révocation, les modifications humaines et l’absence de boucle entre agents. La validation d’un service permanent et de plusieurs comptes réels reste distincte de cette commande ponctuelle.

Si la création distante d’une question a réussi mais que son accusé de réception a été perdu, reprendre d’abord normalement le même drain : il retrouve la demande par sa clé originale. Si l’opération a déjà été abandonnée sans avoir appris l’identifiant de question, la réconciliation locale retourne `needs_review / human_request_unresolved` et conserve le lot. Pour rapprocher explicitement cette demande en lecture seule :

```sh
python3 -m blackoutburst_agents events \
  --journal /chemin/prive/evenements \
  --work-journal /chemin/prive/journal-board \
  reconcile --batch identifiant-lot --operation identifiant-operation-worker \
  --connection-file /chemin/prive/connexion-board.json
```

Cette option utilise exactement la session, la portée et la question d’origine. Elle peut uniquement enregistrer une demande existante vérifiée, puis clôturer le lot déjà abandonné ; elle ne crée pas de question et ne reprend pas le modèle. Une réponse tardive reste alors associée à ce lot. Une demande absente, inaccessible ou différente ne clôt rien : son absence ponctuelle ne prouve pas l’arrêt d’un ancien envoi ambigu.
