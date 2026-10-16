# Répondre aux messages avec un hôte explicite

Le mode `schema_version: 2` de l’hôte utilise le flux confirmé du board, disponible
avec la portée Runtime `message` du board 2.8 / schéma 10. Il répond en texte aux
messages humains adressés au profil dans des conversations choisies localement,
y compris les discussions de tickets. Il ne crée, ne prend, ne modifie ni ne
termine de ticket. Le mode `schema_version: 1` conserve son fonctionnement
[d’activité de tickets](HOST.md). Les deux modes sont configurés séparément.

La création reste désactivée. `enable` autorise seulement le budget fini ; `run`
est une commande distincte au premier plan. Ni une installation, ni un quota
réinitialisé, ni un message reçu ne démarre le processus. Aucun service ou
listener permanent n’est installé. Une fois le processus explicitement lancé,
il vérifie le flux à l’intervalle configuré. Un Mac éteint ne reçoit pas de
réponses de cet hôte ; l’exécution Linux du développement n’est pas qualifiée
par ce mode textuel.

## Configuration choisie par le propriétaire

Le fichier et le journal restent privés (`0600` et `0700`). `connection_file`
désigne une connexion déjà appariée ; aucun secret ne figure dans cette
configuration. Identités, session, version du profil, conversations, instruction
et modèle sont explicitement choisis. Une cible correspond à un profil/session ;
elle possède un seul feed partagé entre ses conversations.

```json
{
  "schema_version": 2,
  "owner_id": "OWNER_ID",
  "board_endpoint": "https://board.example/mcp",
  "interval_seconds": 15,
  "max_pages": 2,
  "page_limit": 50,
  "dispatch_budget": 2,
  "targets": [
    {
      "id": "assistant-personnel",
      "agent_id": "AGENT_ID",
      "board_session_id": "SESSION_ID",
      "profile_version": 1,
      "connection_file": "/absolute/private/connection.json",
      "instruction": "Répondre en français à la question humaine, de manière concise.",
      "workspace": "/absolute/workspace",
      "executable": "/absolute/path/to/codex",
      "model": "MODEL_EXPLICITLY_AVAILABLE_TO_THIS_OWNER",
      "timeout": 120,
      "thread_ids": ["CONVERSATION_ID", "TICKET_DISCUSSION_ID"],
      "start_at": "now"
    }
  ]
}
```

Les commandes `host configure`, `status`, `enable`, `tick`, `run`, `stop` et
`reconcile` sont celles du [guide de l’hôte](HOST.md). Utiliser la révision
actuelle pour `enable`. La configuration normalisée est immuable ; changer le
JSON initial ne change pas le journal. Le verrou canonique du propriétaire
conserve le chemin et l’UUID exacts de l’hôte.

`start_at: now` fixe la frontière dès la première ouverture distante ; une
réponse d’ouverture perdue ne la recalcule pas. `beginning` inclut délibérément
les messages adressés historiques. Une clé d’ouverture privée est enregistrée
avant ce premier appel. Changer de profil/session, de connexion, de bornes ou
de conversations exige une réconciliation explicite ; ce lot ne migre pas
silencieusement les feeds.

Bornes : 1–16 profils distincts, 1–32 conversations par profil, 1–20 nouvelles
opérations par activation, 5–300 secondes entre passages, 1–4 pages de 1–200
messages, 1–300 secondes par tour. Un tick lance au plus une opération. Ce mode
exige un modèle explicite ; il n’invente pas de fournisseur, ne délègue pas et
n’utilise aucun secours payant. Les outils d’exécution du transport personnel
restent désactivés. Les réponses de plus de 8 000 octets UTF-8 sont conservées
pour revue et ne sont pas tronquées puis publiées automatiquement.

## Source, contexte et autorisation

Le feed livre seulement les messages humains adressés selon les règles du
board. Les bots ne déclenchent pas de réponse ; le client refuse aussi un bot
retourné contrairement au contrat. Le routage est limité aux `thread_ids`
choisis. Un message d’une autre conversation autorisée est enregistré comme
ignoré puis acquitté, sans modèle ni budget. Ajouter cette conversation plus
tard ne rejoue pas rétroactivement ces messages.

Le worker relit `get_message` pour la source exacte, même si elle est plus
ancienne que les messages récents. Il vérifie son ID, sa conversation, son auteur
humain et l’empreinte du contenu reçu. Le `source_sha256` fourni par le serveur
couvre la projection immuable brute : `id`, `thread_id`, `body`, `author`,
`created_at`, `reply_to`, `mentions`, `attachment_ids`. URL, noms de mentions
actuels et métadonnées actuelles de pièces jointes sont exclus. Cette empreinte
est une référence, jamais une autorisation.

Un contexte frais contient le profil choisi, la source exacte, la conversation
et jusqu’à 50 messages récents, avec les métadonnées du ticket lié lorsque
accessibles. La source est dédupliquée dans cet historique. Le prompt est borné
à 60 000 octets ; les omissions d’historique sont signalées. Une source ou un
brief ne tenant pas dans cette borne bloque le départ. Aucune pièce jointe,
URL ou chemin n’est ouvert ; aucune mémoire de ticket n’est étendue
implicitement à une conversation générale.

Les références du profil, de la conversation, du projet/ticket éventuel et des
seuls messages effectivement inclus sont scellées avec leurs empreintes. Chaque
reprise et chaque publication relisent ces sources exactes ; le contrôle est
répété après l’admission Runtime, avant le premier appel au transport modèle.
Un retrait d’accès ou une modification bloque le travail pour revue, sans
remplacer son prompt par un contexte différent. Les nouveaux messages ne sont
pas des sources de l’ancien prompt : notre propre publication dont le reçu a
été perdu peut ainsi être rapprochée sans invalider son contexte.

Pendant le modèle, les callbacks de contrôle relisent au plus toutes les cinq
secondes le profil, la source et les racines conversation/projet/ticket. Les
50 références historiques au maximum sont relues intégralement aux frontières
du modèle et de la publication. Un échec de ce contrôle reste une annulation
pour ce tour, même si l’accès revient ensuite. Ces lectures sont bornées ; elles ne constituent
pas une transaction distante couvrant la durée d’un modèle ou d’un post. Un
ancien journal prototype sans ces références est refusé pour revue.

L’identité réelle owner/profil/appareil/session est conservée dans la connexion
et Runtime. La nouvelle réservation `reserve_message_execution` est ancrée au
message et à sa conversation. Le serveur vérifie les droits actuels
`allow_read`/`allow_reply`, le profil actif/versionné, l’appareil, l’appartenance,
la portée du profil, l’adressage humain et le hash de source à admission,
démarrage et contrôle. Cette portée ne confère aucun droit de tâche. Elle
partage la capacité du propriétaire avec les réservations de tickets ; pause,
arrêt, heartbeat et nettoyage gardent le même protocole.

Le RunStore privé de réponses est séparé dans `message-runs`. Son alias local
`board-message` est scellé avec la portée `message`, l’origine, la session, le
profil, la conversation, la source et son hash. Les colonnes locales historiques
`project_id`/`ticket_id` y servent uniquement de coordonnées opaques
conversation/message : elles ne sont jamais envoyées comme tickets au serveur
et aucun ticket factice n’est créé. Chaque source a un nouveau work UUID
préalloué et une session fournisseur neuve. Aucun ancien dialogue fournisseur
n’est adopté, ni réutilisé entre conversations ou propriétaires.

## Durabilité et publication

Une livraison et ses références entrent ensemble dans le journal SQLite privé.
L’hôte débite ensuite son budget et préalloue l’UUID exact du worker dans la même
intention. Le work UUID du RunStore était également choisi avant sa création.
Un enfant/work existant avec un autre ID est refusé, même vide. Un état modèle
manquant après une intention `running` ne devient jamais un droit de recréer le
travail.

Le prompt, les identités et l’intention `running` sont durables avant le tour.
Le RunStore conserve le résultat privé avant la publication. Une reprise utilise
la même request et rapproche les reçus Runtime ; elle ne relance pas une
inférence déjà terminée. Une inférence interrompue sans résultat confirmé reste
pour revue. Ces garanties concernent les coupures normales de ces journaux,
pas une restauration partielle ou un rollback arbitraire de fichiers.

Avant publication, connexion, accès et toutes les sources scellées du contexte
sont vérifiés de nouveau.
Le seul effet est `post_message` dans la conversation d’origine avec le
`reply_to` exact et une clé idempotente stable par owner/profil/source. Le corps
retourné, l’auteur, la session, l’appareil et les références doivent correspondre
exactement avant d’enregistrer le reçu local. Une réponse de publication perdue
répète ce même post et sa clé, sans nouveau modèle.

L’ACK du feed intervient seulement après les reçus durables de tous les messages
de la livraison : réponses confirmées ou décisions explicites d’ignorer les
conversations non sélectionnées. Un budget épuisé au milieu d’un lot désactive
l’hôte et conserve le lot non acquitté ; un nouvel `enable` explicite reprend les
messages restants. Une réponse d’ACK perdue est rapprochée avec le même
`delivery_id`/checkpoint, avant toute nouvelle lecture. Un retrait d’accès ne
provoque jamais un ACK silencieux ni une nouvelle publication.

## Journaux, récupération et limites

Les sous-dossiers sont `messages`, `message-runs` et `message-runtime`. Les
journaux contiennent des données privées de travail ; `status` et les reçus
publics n’en exposent ni prompts, ni texte modèle, ni chemins, ni secrets.
Le journal de messages accepte au plus 256 opérations retenues et 256
livraisons, avec 256 Kio maximum de données privées par opération (prompt,
références et résultat compris). Les opérations terminées sont conservées pour preuve. Après un ACK
confirmé, les opérations ignorées sont retirées ; les anciennes livraisons
acquittées ne contenant aucune réponse sont compactées en conservant la plus
récente et le checkpoint. Mille pages vides ne remplissent donc pas le journal.
Aucune opération non acquittée ou réponse terminée n’est supprimée. Une limite
atteinte conserve le lot et impose une réconciliation humaine ; aucun reset
automatique ou UI de purge n’est fourni.

La commande `runtime reconcile` sait rapprocher le nettoyage d’une portée
message depuis son résultat privé exact, sans réservation, modèle ou
publication supplémentaires. Après retrait de droits, seul l’appareil/session
original encore valide peut attester ce nettoyage ; un appareil révoqué reste
refusé. `host reconcile --batch ...` demeure hors connexion : il confirme
seulement un enfant déjà publié et exactement lié. Il n’abandonne aucun travail,
ne force aucun ACK distant et ne réactive rien.

Ne pas supprimer les journaux ou leur verrou pour contourner une incertitude.
Arrêter les coordinateurs avant une migration/restauration et conserver une
copie privée assortie du code et de tous les journaux. Un reset logique n’efface
pas les messages du board, les prompts déjà exportés ou les historiques du
fournisseur. Le lot ne promet ni antirollback OS ni service cloud permanent.

Les tests utilisent le vrai Board temporaire, SQLite et des modèles factices.
Ils couvrent les deux types de conversation, les pertes de réponses, les
coupures aux frontières durables, les enfants étrangers, le budget partiel,
les sources/accès retirés, les bots, les capacités et l’absence d’effets sur les
tickets. Aucun modèle, compte, serveur de production ou listener n’a été lancé
pour ces validations.
