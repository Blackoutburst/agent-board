# Sélectionner un ticket dans un projet

`queue run` prévisualise les tickets accessibles d’un projet. Avec `--execute`, il réserve au plus un ticket éligible, appelle le worker textuel et termine. Il ne boucle pas sur la file. Le propriétaire doit avoir autorisé la gestion des tickets et la sélection dans la file ; le profil doit également l’autoriser. Le modèle est obligatoire et fixé dans l’opération.

```sh
python3 -m blackoutburst_agents queue \
  --journal /chemin/prive/file \
  --work-journal /chemin/prive/journal-board \
  --state /chemin/prive/sessions-codex \
  run --connection-file /chemin/prive/connexion.json \
  --session session-autorisee --owner proprietaire --agent identifiant-agent \
  --project identifiant-projet --request demande-unique \
  --instruction-file /chemin/prive/instruction.txt \
  --workspace /chemin/espace-vide --codex /chemin/codex --model modele-autorise
```

Sans `--execute`, seuls l’enregistrement non autonome et les lectures du projet et de la file sont effectués. Aucun journal, réservation ou modèle n’est créé. Ajouter `--execute` constitue une demande ponctuelle de sélection. `--human-owner identifiant-proprietaire` autorise le même parcours question/réponse que [`worker`](WORKER.md), avec destinataire immuable. Relancer la même commande reprend ce ticket ; une réponse humaine ne sélectionne jamais un autre ticket.

Une question dont la réservation a été libérée bloque son ticket côté serveur, tout en permettant une sélection explicite pour un autre ticket prêt. Le journal local de `queue` reste séquentiel : une opération de file encore inachevée, y compris en attente humaine, empêche un nouveau cycle dans ce journal. Une sélection distincte ne répond pas à la question précédente et ne reprend pas son modèle.

## Un travail à la fois

Le serveur choisit atomiquement selon l’affectation, les dépendances, la priorité et les permissions courantes. Une réservation inachevée, même expirée, empêche de sélectionner un nouveau ticket, sur les autres sessions et appareils du profil aussi. Une demande humaine dont la réservation a été libérée bloque uniquement son propre ticket. La réservation explicite d’un ticket reste une opération séparée.

Le journal privé fixe le propriétaire, le profil, la connexion, la session, le projet, le modèle et l’instruction. Ses clés de sélection et d’exécution sont écrites avant les effets. Un verrou partagé entre projets et sessions du même profil empêche de contourner une opération encore ouverte. Les chemins doivent désigner des répertoires privés appartenant à l’utilisateur (`0700`). Conserver les mêmes chemins pour la reprise.

La sélection positive comme négative possède un reçu durable sur le board. Une réponse réseau perdue conduit à un examen ; la relance ne choisit pas un autre ticket. L’exécutant enfant est alloué avant son lancement et son identifiant reste immuable. Une publication `review` confirmée termine l’opération. Un résultat `blocked`, un quota ou une incertitude ne libère pas la file locale pour un autre travail.

## Examiner une interruption

```sh
python3 -m blackoutburst_agents queue --journal /chemin/prive/file \
  status --operation identifiant-operation
python3 -m blackoutburst_agents queue --journal /chemin/prive/file \
  inspect --operation identifiant-operation --output /chemin/prive/examen.json
```

Le statut contient uniquement des métadonnées. L’export privé `0600` contient les instructions et les effets enregistrés ; ne pas le publier.

Si la sélection est incertaine, retrouver son reçu exact en lecture seule :

```sh
python3 -m blackoutburst_agents queue --journal /chemin/prive/file \
  reconcile-selection --operation identifiant-operation \
  --connection-file /chemin/prive/connexion.json
```

Cette commande utilise la connexion, la session, le projet et la clé de l’opération d’origine. Elle ne sélectionne ni ne lance rien. Un reçu absent après un envoi tenté ne prouve pas l’absence d’effet ; l’opération reste ouverte pour examen.

Si le worker enfant a déjà été traité, le rapprocher localement :

```sh
python3 -m blackoutburst_agents queue --journal /chemin/prive/file \
  --work-journal /chemin/prive/journal-board \
  reconcile-worker --operation identifiant-operation
```

Il faut la preuve du même enfant, avec la même configuration. Un enfant en attente, un autre journal vide ou une publication bloquée ne devient pas un succès. Examiner et résoudre d’abord l’enfant avec les commandes `worker` et `runner`. Un enfant alloué sans aucun effet, encore à l’étape `new`, peut être abandonné explicitement avec `worker abandon`.

Après vérification des effets sur le board et traitement de sa réservation, l’opérateur peut abandonner localement :

```sh
python3 -m blackoutburst_agents queue --journal /chemin/prive/file \
  --work-journal /chemin/prive/journal-board \
  abandon --operation identifiant-operation --revision revision-examinee \
  --reason "Effets vérifiés et réservation traitée sur le board" \
  --acknowledge-board-reconciled
```

Cette commande ne contacte pas le board et ne lance pas de modèle. Elle vérifie la sélection et la preuve enfant avant de conserver l’intention. En cas de coupure pendant sa transition, répéter exactement la même révision initiale et la même raison. Les anciennes clés restent des tombes durables, sans nouvel effet.

## Validation

Les tests couvrent le serveur réel sur base temporaire, les refus de permission, la sélection atomique, les changements de profil/session, les réponses perdues, les interruptions et les journaux enfants. Les modèles sont simulés pour ce parcours. Aucune sélection réelle de file ni écoute permanente n’a été activée en production par cette livraison.
