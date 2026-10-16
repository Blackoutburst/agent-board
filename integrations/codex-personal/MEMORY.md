# Mémoire locale de décisions explicitement épinglées

Cette option rappelle une décision ancienne d’un ticket sans parcourir son historique complet. Le propriétaire choisit un message humain exact et rédige le résumé à conserver. Aucun modèle ne choisit les décisions, aucun embedding ou service externe n’est utilisé. Le résumé reste une donnée citée, jamais une instruction ou une autorisation.

La mémoire est liée à l’origine MCP configurée et aux quatre identifiants **propriétaire, agent, projet, ticket**. Elle ne s’étend pas implicitement à un autre ticket ou au projet entier. Une connexion fraîche doit confirmer cette identité et l’accès au ticket avant le pin. La source est relue par `get_message(thread_id, message_id)` dans la conversation exacte du ticket ; les liens, chemins, fichiers joints et commandes présents dans un message ne sont jamais suivis.

La séparation reste explicite : les préférences personnelles viennent du profil autorisé ; les faits partagés viennent des sources MCP et de ces résumés approuvés pour le ticket ; les prompts, réponses et états temporaires restent dans ContextCache/WorkJournal/RunStore. Aucun contenu privé d’un autre propriétaire n’est cherché ou copié. Le fichier de résumé est une entrée locale choisie par le contrôleur ; son nom et le chemin du magasin n’entrent pas dans le contexte du modèle.

## Épingler et corriger

Utiliser un dossier privé canonique, sans lien symbolique dans ses ancêtres, et un fichier de résumé UTF-8 explicitement choisi. Le programme crée le magasin en mode 0700 et ses fichiers en mode 0600. Une lecture n’initialise jamais un magasin absent.

```sh
blackoutburst-agents memory --state /absolute/private/decisions pin \
  --owner OWNER --agent AGENT --project PROJECT --ticket TICKET \
  --endpoint https://board.example/mcp \
  --connection-file /absolute/private/connection.json --session SESSION \
  --thread THREAD --message MESSAGE --summary-file /absolute/summary.txt --revision 0
```

`--revision 0` sert au premier pin de cette portée. Les suivants utilisent la révision inspectée. Le reçu contient l’UUID de la décision, la génération, la révision et des empreintes, sans texte privé. Pour corriger explicitement un résumé ou accepter une source modifiée, répéter `pin` avec `--decision UUID` et la révision courante. Une nouvelle décision sur une source déjà épinglée est refusée ; une nouvelle tentative identique ne duplique ni le pin ni sa révision.

Chaque pin conserve la référence MCP exacte et le SHA256 de la projection complète autorisée du message, avant toute troncature d’affichage. Les champs inconnus, identifiants de connexion, fichiers joints et métadonnées non nécessaires ne sont pas conservés. La source doit être humaine et appartenir à ce ticket.

## Consulter, retirer et réinitialiser

```sh
blackoutburst-agents memory --state /absolute/private/decisions inspect \
  --owner OWNER --agent AGENT --project PROJECT --ticket TICKET \
  --endpoint https://board.example/mcp --output /absolute/private/memory.json

blackoutburst-agents memory --state /absolute/private/decisions forget \
  --owner OWNER --agent AGENT --project PROJECT --ticket TICKET \
  --endpoint https://board.example/mcp --decision UUID --revision REVISION

blackoutburst-agents memory --state /absolute/private/decisions reset \
  --owner OWNER --agent AGENT --project PROJECT --ticket TICKET \
  --endpoint https://board.example/mcp --revision REVISION
```

Ces trois commandes sont locales, sans MCP ni modèle. `forget` retire le pin choisi ; `reset` retire tous les pins de cette portée. Elles augmentent une génération durable et suppriment les anciennes révisions du magasin mémoire. Une tombstone vide est conservée : un ancien contexte inspecté, un prompt enregistré ou une nouvelle tentative ne peut pas rétablir automatiquement ces pins.

**Le reset est logique, pas un effacement global des traces.** Les anciens exports, ContextCache, journaux d’opération, prompts, réponses et historiques du fournisseur peuvent encore contenir l’ancien contexte en tant que traces privées d’audit. Ils ne deviennent pas des sources autorisées pour réépingler automatiquement la mémoire. Le message source reste sur le board et peut toujours apparaître dans l’historique récent normal, s’il est encore autorisé : retirer son pin ne supprime pas cette conversation. La commande existante `context --state CACHE forget --key KEY --fingerprint FINGERPRINT` permet d’effacer explicitement les révisions locales d’un contexte ; les exports et historiques nécessitent leurs procédures propres.

Une restauration manuelle d’un ancien magasin mémoire ou RunStore restaure aussi ses anciens états : le mécanisme ne promet pas une protection contre le rollback du système de fichiers ou les modifications manuelles de SQLite. Après une restauration, le propriétaire doit réconcilier les resets, effets et sessions avant toute reprise. Ne pas retirer les marqueurs pour forcer une continuation.

## Utiliser un contexte inspecté

Ajouter `--decision-memory /absolute/private/decisions` aux commandes `context prepare`, `context check` et `context prompt`. Le magasin est toujours fourni explicitement par le contrôleur ; le contexte ne contient aucun chemin permettant de le découvrir ou de l’ouvrir automatiquement.

Un contexte épingle l’UUID du magasin, sa portée, sa génération, sa révision et son empreinte. `verified_context` exige le même magasin courant avant toute utilisation. Il relit le profil/ticket/contexte et chacune des sources exactes. La source ancienne reste consultable hors de la fenêtre de messages récents : un pin coûte une lecture exacte, au maximum huit par composition. Toute correction/retrait/reset, modification de source, perte d’accès, changement de profil ou de ticket bloque un ancien contexte avant le prochain tour. Un échec de lecture ne signifie jamais « source inchangée ».

Le contexte garde sa limite globale de 48 000 octets et signale ses omissions. Un résumé peut être tronqué pour l’affichage, mais son empreinte et celle de sa source portent sur les valeurs complètes. Les données de mémoire ne remplacent aucune instruction du propriétaire, politique du profil ou autorisation du serveur.

## Exécution et reprise

`worker run --decision-memory /absolute/private/decisions` épingle le descripteur dans la configuration durable de l’opération, sans chemin privé. La reprise après réponse humaine relit les pins et leurs sources avant le second tour. Un changement demande une revue ; il ne remplace pas la mémoire en cours silencieusement. Le callback d’annulation vérifie aussi les changements locaux pendant un tour actif ; le nettoyage reste vérifié par le runner et Runtime, sans promettre d’annuler un effet déjà réalisé.

Le worker inclut le descripteur dans l’identité de sa session locale. Une nouvelle révision/génération utilise donc un nouveau travail et une nouvelle session fournisseur ; la reprise avec le même pin garde la session exacte d’origine. Retirer l’option sur une opération déjà épinglée crée un conflit de configuration et ne relance rien.

Pour `runner turn`, ajouter `--decision-memory` en plus des cinq options de contexte inspecté. Un binding durable dans **RunStore** est vérifié avant tout tour, même si l’option ou tout le contexte est omis ensuite. Un travail déjà marqué exige le même descripteur. Après reset/correction, clôturer le travail précédent par la procédure de réconciliation existante et créer un travail neuf ; le programme n’adopte pas une session fournisseur historique non liée. Cela empêche une ancienne session de réintroduire la décision retirée par sa conversation passée. Les transcriptions précédentes ne sont pas effacées.

Arrêter les anciens coordinateurs partageant le magasin avant de l’ouvrir avec cette version, puis les relancer avec le code assorti. Un ancien processus ayant déjà ouvert la base ne reçoit pas rétroactivement les nouveaux contrôles. Le format privé RunStore passe de 1 à 2 par une migration additive transactionnelle qui conserve les travaux et tours existants. Les anciennes versions de l’adaptateur refusent ce format ; un rollback exige l’état privé correspondant. Le marqueur est dans la même base que la session fournisseur, pas dans un fichier optionnel dont l’absence ferait passer le travail pour non gardé. La réconciliation Runtime d’un résultat déjà enregistré reste possible après reset : elle utilise ce binding historique pour confirmer uniquement le nettoyage, sans inférence ni publication.

Sans option mémoire, aucune décision ni lecture `get_message` n’est ajoutée au contexte. Les hôtes, événements et files n’activent pas cette option automatiquement. Le noyau de développement reste désactivé et aucun listener n’est créé.

## Bornes et limites

Le magasin accepte au maximum 64 portées, 8 décisions par portée, 2 048 octets par résumé, 20 000 octets par photographie et dix révisions conservées. Retrait/reset purge les anciennes révisions de la portée tout en conservant sa génération. Le magasin mémoire et RunStore gardent chacun au maximum 256 bindings de travaux ; aucune suppression ou migration automatique ne contourne ces limites.

`get_message` relit seulement une source autorisée et refuse une réponse supérieure à 256 KiB UTF-8. La mémoire revalide aussi cette borne ; aucune lecture générale du board ou de pièce jointe ne sert de repli.

Il s’agit de décisions choisies explicitement, sans recherche sémantique automatique. Les empreintes détectent un changement, elles ne prouvent ni la vérité du résumé ni sa fidélité sémantique au message. Les lectures MCP et la mémoire locale ne forment pas une transaction distribuée : les contrôles de fraîcheur et d’arrêt réduisent les fenêtres de changement sans certifier une photographie atomique de tous les systèmes.
