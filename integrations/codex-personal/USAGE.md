# Limites de la connexion Codex locale

`usage` consulte les limites exposées par le client officiel connecté au compte ChatGPT du propriétaire. Aucun tour de modèle, connexion, achat, reset ou changement de quota n’est lancé. La sonde refuse l’authentification API et ne demande pas de bascule vers un autre modèle. [Protocole App Server officiel](https://learn.chatgpt.com/docs/app-server)

## Lecture explicite

Depuis ce dossier :

```sh
python3 -m blackoutburst_agents usage read --codex /chemin/absolu/codex \
  --output-private /chemin/prive/usage.json
```

Le terminal affiche seulement l’état, la date et le nombre de compartiments reçus. Ajouter `--show` affiche explicitement les valeurs du compte local. Le fichier de sortie privé contient uniquement l’observation filtrée ; il est écrit en mode 0600. Ne pas le placer dans Git ou le joindre à une conversation partagée.

La sonde ouvre un App Server local temporaire, vérifie le mode ChatGPT puis lit `account/rateLimits/read`. Elle borne la durée à 30 secondes par défaut (maximum 60) et la sortie à 4 Mio, ignore les notifications partielles, refuse les appels entrants et termine son groupe de processus. Une réponse non prise en charge, trop volumineuse ou un nettoyage incertain produit un état indisponible. Les données brutes, identifiants de compte, informations d’offre, crédits et détails de reset ne sont pas enregistrés. L’environnement de connexion reste celui du propriétaire ; aucun fichier d’authentification n’est lu ou copié par l’adaptateur.

## Transmission au board

Le fichier de connexion MCP privé est celui créé par le connecteur du board. Avec une session autorisée pour l’agent et le propriétaire indiqués :

```sh
python3 -m blackoutburst_agents usage report \
  --connection-file /chemin/prive/board.json \
  --session ma-session --owner mon-owner-id --agent mon-agent-id \
  --observation-file /chemin/prive/usage.json
```

Le serveur vérifie la session et le binding courant, puis stocke une observation privée pour cette connexion. Une reprise du même envoi n’allonge pas sa fraîcheur ; les observations antérieures sont refusées. Aucun droit, pause, réservation ou choix de modèle n’est modifié.

## Lecture dans Runtime

Dans **Agents → Runtime**, les compartiments et fenêtres restent séparés. Une valeur absente reste inconnue ; zéro pour cent utilisé vaut cent pour cent restant. L’observation devient périmée après deux minutes au plus. La date est celle de la lecture locale, car le fournisseur ne garantit pas la date de mesure ni la fraîcheur de son cache. Un reset passé n’établit pas que le quota a été renouvelé.

Le hash de connexion lie le board, le propriétaire, l’agent et l’appareil. Il ne prouve pas l’identité du compte ChatGPT : deux appareils du même propriétaire peuvent utiliser des comptes différents, et plusieurs connexions peuvent partager le même abonnement. Les pourcentages ne sont donc jamais additionnés. `ordinary_usage_allowed`, lorsqu’il existe, est une observation nullable du fournisseur et ne vaut pas autorisation du board. Un blocage d’exécution observé reste prioritaire jusqu’à la reprise explicite du propriétaire.

Ces commandes sont ponctuelles : aucun listener ni rafraîchissement automatique n’est activé.
