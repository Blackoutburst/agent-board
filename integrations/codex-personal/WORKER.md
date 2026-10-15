# Un ticket choisi, une réponse en revue

`worker run` relie un ticket explicitement choisi à un tour de texte Codex. Il réserve le ticket, compose et conserve ses sources, exécute l’instruction locale, puis ajoute le résultat et sa preuve dans le ticket avec le statut `review`. Un échec ordinaire du modèle produit un constat borné avec le statut `blocked`. Le programme libère sa réservation lors de cette publication. Il ne déclare jamais le ticket terminé.

Ce parcours ne modifie pas le dépôt de travail et n’utilise aucun outil de développement. Il sert aux demandes d’analyse ou de rédaction. La création des profils et le choix des fournisseurs restent gérés par le board ; ce module réutilise la connexion MCP du propriétaire. Il ne démarre aucun listener et ne choisit aucun ticket dans la file.

## Lancer une demande

Préparer un fichier d’instruction locale, par exemple « Résume les critères de validation de ce ticket et signale les informations manquantes ». Le contenu du board sera fourni comme source non fiable, séparément de cette instruction. Les chemins de journal et d’état doivent être privés ; les répertoires existants doivent appartenir à l’utilisateur avec des permissions `0700`.

Depuis ce dossier :

```sh
python3 -m blackoutburst_agents worker \
  --journal /chemin/prive/journal-board \
  --state /chemin/prive/sessions-codex \
  run \
  --connection-file /chemin/prive/connexion-board.json \
  --session session-stable-de-ce-travail \
  --owner identifiant-proprietaire \
  --agent identifiant-agent-du-board \
  --project identifiant-projet \
  --ticket identifiant-ticket \
  --request analyse-ticket-001 \
  --instruction-file /chemin/prive/instruction.txt \
  --workspace /chemin/dossier-de-travail \
  --codex /chemin/absolu/codex \
  --timeout 120
```

Cette commande consomme le quota de la connexion ChatGPT personnelle et publie sa réponse sur le ticket partagé. Le propriétaire doit avoir autorisé ce travail et la publication. Le fichier de connexion est celui du `connect.py` commun, de permissions `0600`. Le client refuse les outils MCP autres que lecture du ticket/conversation, réservation, renouvellement et mise à jour bornée du résultat. Aucun jeton ne rejoint le prompt, le journal ou le ticket. Les permissions serveur restent contrôlées à chaque appel.

`--model` permet de choisir explicitement un modèle accessible ; sans cette option, le client conserve son modèle par défaut. Aucun remplacement automatique ni repli API payant n’est prévu. Le délai va de 1 à 300 secondes. L’instruction est limitée à 12 000 octets UTF-8 ; les sources et le prompt sont bornés séparément. Une réponse trop grande pour le champ du board ou la requête MCP arrête la publication et demande une revue.

## Poser une question puis reprendre

Avec le board **2.0.3 ou supérieur**, ajouter `--human-owner identifiant-proprietaire` autorise une seule question d’information à ce participant du projet. Le destinataire est fixé par cette option locale. Le modèle ne peut pas choisir une autre personne. Sans cette option, le parcours conserve son comportement textuel habituel et son client MCP ne permet aucune question.

Si une information manque, l’exécutant enregistre son intention puis envoie la question dans le ticket. Le board mentionne le destinataire, bloque le ticket et libère le bail tout en conservant l’agent chargé de reprendre. La commande retourne `pending`, étape `waiting_human`, puis termine. Aucun modèle ni processus d’attente ne reste actif.

Le destinataire répond à la demande humaine dans le board. Un message de conversation ordinaire ne résout pas cette demande. Relancer ensuite **exactement la même commande**, avec la même option `--human-owner` et la même clé `--request`. L’exécutant retrouve la réponse, revalide les permissions et l’affectation, réserve de nouveau le ticket, recompose son contexte et reprend la même session Codex. Ce second tour doit produire le résultat final, soumis en revue. Une autre demande ouverte ou une dépendance maintient l’attente ; une réaffectation ou une réservation concurrente arrête la reprise pour examen.

La création compare atomiquement la version du ticket à celle du contexte analysé. Une modification concurrente empêche l’envoi de la question et conserve la réservation pour examen. Les tentatives identiques retrouvent la même question grâce à sa clé durable. La consultation utilise l’outil distinct `lookup_human_request`, qui n’écrit rien. Un ancien serveur ne connaissant pas cet outil refuse la consultation ; le client ne tente pas de créer une question pour simuler cette lecture. La question est limitée à 6 000 octets UTF-8 et la réponse humaine à 12 000 ; les dépassements demandent une revue, sans troncature silencieuse. Une modification du profil pendant l’attente impose une revue avant toute nouvelle réservation ou inférence. Une seule continuation est autorisée : les demandes d’action ou d’approbation ne font pas partie de ce parcours. La réponse est une source non fiable, jamais une autorisation d’exécuter du code ou d’élargir la tâche.

À la première question, le journal passe atomiquement en version 2. L’adaptateur 0.5 refuse ensuite ce journal ; utiliser l’adaptateur 0.6 ou supérieur pour le reprendre ou l’examiner. Les journaux utilisés uniquement pour des réponses directes restent en version 1. Le contexte initial conserve sa preuve condensée ; le contexte actif complet et les tours de la session restent dans les bases privées.

## Reprise et concurrence

Après une coupure, relancer exactement la même commande : mêmes répertoires privés, connexion, session, identifiants, clé de requête et configuration. La clé désigne une opération immuable. La changer ne permet pas de contourner une opération encore incertaine.

Le journal conserve l’intention avant chaque effet externe. Une réservation dont la réponse a été perdue est reconnue par sa version et son propriétaire exact. Un résultat Codex déjà enregistré est réutilisé. Une publication dont la réponse a été perdue est reconnue par son contenu complet, son marqueur d’opération, son statut et la libération de la réservation. Une modification humaine concurrente ne peut pas être écrasée silencieusement.

Un verrou local protège la portée complète : propriétaire, agent, projet, ticket, URL du board et session MCP. Le modèle utilise également une identité locale dérivée de cette connexion et de cette session ; une autre connexion ne reprend pas accidentellement sa conversation. La réservation du board empêche un second exécutant de travailler simultanément sur le même ticket.

Les sources, les permissions et la réservation sont relues avant le modèle et avant la publication. Le bail doit encore couvrir le délai du modèle plus dix secondes. Le programme ne récupère pas une réservation expirée et ne prolonge pas automatiquement les baux. Les touches d’interruption demandent l’arrêt du processus Codex détenu par l’exécutant ; aucune interruption n’est envoyée à un PID arbitraire enregistré.

## États et contrôle local

Le reçu standard contient uniquement l’identifiant d’opération, son état et un code de diagnostic. `complete` signifie que la publication a été confirmée, y compris lorsque le ticket publié est `blocked`. `pending` signifie qu’une tentative identique peut réconcilier un effet non confirmé. `needs_review` exige un examen explicite. Aucun de ces états ne garantit que les critères métier du ticket sont satisfaits.

```sh
python3 -m blackoutburst_agents worker \
  --journal /chemin/prive/journal-board \
  status --operation identifiant-operation
```

Cette lecture locale ne se connecte pas au board et ne lance pas de modèle. Les sources, instructions, réponses et intentions de publication sont conservées dans les bases privées. Les outils de statut ne les impriment pas. Leur conservation sert à la reprise ; les supprimer ferait perdre la capacité de reconnaître une opération déjà effectuée.

## Examiner puis abandonner une opération incertaine

Une opération `needs_review` empêche de démarrer une nouvelle demande dans la même portée. Pour l’examiner, exporter ses détails dans un fichier privé :

```sh
python3 -m blackoutburst_agents worker \
  --journal /chemin/prive/journal-board \
  inspect --operation identifiant-operation --output /chemin/prive/examen.json
```

Le fichier `0600` contient les instructions, sources, résultat et intentions enregistrées. Ne pas le publier comme un reçu technique. Comparer ces éléments au ticket réel et traiter explicitement sa réservation ou une publication éventuellement déjà effectuée. L’abandon local n’annule aucun effet distant.

Après cet examen, l’opérateur peut confirmer qu’il a réconcilié le board et abandonner l’opération :

```sh
python3 -m blackoutburst_agents worker \
  --journal /chemin/prive/journal-board \
  --state /chemin/prive/sessions-codex \
  abandon --operation identifiant-operation --revision revision-examinee \
  --reason "Effets vérifiés et réservation traitée sur le board" \
  --acknowledge-board-reconciled
```

Cette commande locale ne contacte pas le MCP et ne lance pas de modèle. Elle ferme la session locale associée, refuse les processus ou groupes encore actifs, puis conserve l’opération dans l’état `abandoned` avec sa raison privée. Elle n’efface ni résultat ni historique. En cas d’interruption de cette commande, la relancer avec la même révision initiale et la même raison ; l’intention enregistrée permet de terminer sans répéter la fermeture.

Après abandon, une nouvelle clé peut désigner une nouvelle demande explicitement autorisée. L’ancienne clé reste un constat terminal et ne redémarre jamais le modèle. Le programme ne transforme pas automatiquement une opération incertaine en succès.

## Validation et limites

Les tests utilisent le vrai Board dans une base temporaire, une inférence simulée et des transports contrôlés. Ils couvrent publication en revue, refus de permission, changement de contexte, concurrence, isolation de propriétaires et connexions, réponses réseau perdues, réouverture du journal, annulation et résultat trop volumineux. Ils ne constituent pas un essai complet de développement autonome sur deux comptes réels.

Le drain durable des événements d’un ticket est documenté dans [EVENTS.md](EVENTS.md). Avec `--human-owner` fixé dès sa création, il conserve et reprend les questions de son propre lot. La sélection des tâches, la délégation et les outils de développement restent des fonctions distinctes. Le listener précédemment arrêté n’est pas réactivé par cette commande.
