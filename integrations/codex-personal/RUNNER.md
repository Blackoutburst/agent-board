# Exécutant personnel local

Le module peut désormais démarrer une conversation Codex, conserver son identifiant et la reprendre dans un autre processus. Chaque entrée est explicitement demandée. Le programme ne sélectionne aucun ticket et n’écoute pas le board.

Cette version traite des **tours de texte avec les outils désactivés**, dans le sandbox en lecture seule. Elle prépare le cycle de vie du connecteur ; les modifications de code, les permissions du board et la publication de réponses restent à intégrer. Elle utilise l’authentification ChatGPT du Codex local, comme la sonde de connexion. Le rejet d’un événement d’outil constate une anomalie ; il ne remplace pas le contrôle d’accès de l’hôte. Les politiques Codex administrées peuvent aussi influer sur l’exécution.

## Créer un contexte

Depuis `integrations/codex-personal`, choisir un dossier de travail existant et un dossier d’état dont le parent existe :

```sh
mkdir -p private
python3 -m blackoutburst_agents runner --state private/runner-state create \
  --owner mon-compte --agent mon-agent --project mon-projet --ticket mon-ticket \
  --workspace /chemin/absolu/du/travail --codex /chemin/absolu/codex
```

La commande renvoie un `work_id`. Les noms de propriétaire, agent, projet et ticket servent à séparer les contextes locaux ; **ils ne prouvent aucune identité et n’accordent aucun droit sur le board**. Utiliser une installation et un compte système propres à chaque propriétaire. Le modèle, le chemin du client et le dossier de travail sont fixés à la création. Répéter la création avec les mêmes paramètres retrouve le contexte ouvert ; un changement incompatible est refusé.

L’option `--model` permet de choisir explicitement un modèle accessible. Sans elle, le client utilise son défaut ; aucun autre fournisseur ou mode de facturation n’est essayé en cas d’erreur.

## Envoyer puis reprendre

Écrire la demande dans un fichier UTF-8 d’au plus 64 Kio. Donner une clé stable à chaque entrée, par exemple l’identifiant de son message :

```sh
python3 -m blackoutburst_agents runner --state private/runner-state turn \
  --work WORK_ID --request message-001 --prompt-file demande.txt \
  --output-text private/reponse.txt
```

Le programme ne place pas le texte de la demande dans les arguments du processus Codex. Son affichage JSON contient le statut et les identifiants locaux, sans la réponse. `--output-text` demande explicitement d’écrire celle-ci dans un fichier privé.

Pour continuer après fermeture ou redémarrage du programme, employer le même `work_id`, une nouvelle clé et un nouveau fichier :

```sh
python3 -m blackoutburst_agents runner --state private/runner-state turn \
  --work WORK_ID --request message-002 --prompt-file suite.txt \
  --output-text private/suite-reponse.txt
```

Seul l’identifiant de session enregistré pour ce contexte peut être repris ; aucune sélection de la conversation « la plus récente » n’est faite. Une nouvelle tentative avec la même clé et le même contenu récupère le résultat déjà enregistré, sans nouvel appel de modèle. Réutiliser la clé avec un autre contenu est refusé. Un verrou empêche deux exécutions simultanées du même contexte.

## Observer et arrêter

```sh
python3 -m blackoutburst_agents runner --state private/runner-state status --work WORK_ID
python3 -m blackoutburst_agents runner --state private/runner-state cancel --work WORK_ID
```

`cancel` enregistre une demande d’arrêt. Le processus actif la vérifie régulièrement et arrête son propre groupe de processus. `Ctrl-C` et `SIGTERM` suivent le même mécanisme. Le statut terminal confirme l’issue : une demande arrivée après la fin effective du tour peut laisser celui-ci terminé. Aucun processus n’est tué à partir d’un ancien PID trouvé dans la base.

Après une coupure brutale ou un échec ambigu, le contexte passe en `needs_review` lors de la tentative suivante. Le programme ne rejoue pas automatiquement la demande. Après examen humain, une continuation explicite peut être autorisée :

```sh
python3 -m blackoutburst_agents runner --state private/runner-state resolve \
  --work WORK_ID --decision continue --reason "Processus terminé et contexte vérifié"
```

La continuation exige une session connue, un processus et son groupe effectivement absents, ainsi qu’un nettoyage qui n’a pas été déclaré incertain. Un PID réutilisé ou inaccessible bloque par prudence. Sans ces preuves, examiner l’exécutant puis choisir explicitement `--decision abandon` pour fermer le contexte. Un lancement interrompu avant l’enregistrement du PID nécessite cette décision humaine ; l’absence de PID ne prouve pas l’arrêt du processus. Une fermeture ne supprime pas l’historique.

## Données et limites

L’état SQLite et les verrous sont locaux et privés (dossier 0700, fichiers 0600). La demande est identifiée par son empreinte ; sa réponse est conservée dans l’état privé. Codex conserve aussi la conversation nécessaire à la reprise dans son propre stockage. Ne pas publier ces données ni les inclure dans un dépôt. Le programme ne lit ni ne copie les fichiers d’authentification.

Les tests automatiques utilisent de faux exécutables. Ils vérifient la reprise, les doublons, les accès concurrents, l’annulation, les erreurs et les coupures. Les preuves réelles, lorsqu’elles sont présentes dans `docs/`, portent seulement sur l’installation personnelle testée. Le second compte, l’hébergement partagé, le renouvellement de connexion et le raccordement aux permissions et événements du board restent à valider.

La reprise utilise le mécanisme officiel [`codex exec resume`](https://learn.chatgpt.com/docs/non-interactive-mode). Les réponses du modèle restent du contenu non fiable : le raccordement au board devra appliquer ses autorisations indépendamment du texte produit.
