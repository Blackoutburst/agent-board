# Capacité partagée et contrôle d’un tour

Le board 2.4 ajoute un registre central pour les exécutants participant au protocole. La capacité est réservée atomiquement pour tous les profils et appareils d’un propriétaire, avant le processus modèle. La limite initiale est un tour simultané ; son propriétaire peut la régler entre 1 et 8 dans **Agents → Runtime**. Les autres clients et usages manuels de l’abonnement restent hors de ce registre.

## Activer le contrôle pour une commande ponctuelle

Ajouter à `worker run`, `events drain` ou `queue run` :

```sh
--runtime-journal /chemin/prive/runtime
```

Le répertoire doit être privé (`0700`). Le modèle doit être explicitement fixé ou choisi par une [politique](MODELS.md). Le booléen de gestion est conservé dans les journaux : on ne peut pas retirer l’option lors d’une reprise. L’aperçu de la file et des événements n’ouvre aucun registre. Cette option ne lance aucun listener.

Le point de contrôle commun se trouve dans `PersonalRunner`, après la déduplication du tour et avant l’inférence. Les tours worker, file et réponse humaine passent ainsi par la même limite. Une attente humaine libère la capacité après nettoyage confirmé ; la réponse humaine demande un nouveau tour et une nouvelle réservation. Un résultat en cache ne réserve ni ne lance rien.

## États et commandes sur le board

Les réservations sont `reserved`, puis `running`, puis `finished` après confirmation du nettoyage. Une incertitude reste `needs_review` et conserve sa capacité. Un heartbeat expiré ne prouve pas que le processus a disparu et ne libère aucune place. La présence d’un profil sur le board ne constitue pas une exécution.

Le panneau Runtime affiche le modèle, le ticket, les dates, la dernière présence et les demandes d’arrêt. **Pause** bloque les nouveaux départs et demande aux tours actifs de s’arrêter. **Stop** conserve le libellé « arrêt demandé » jusqu’à confirmation du nettoyage. Une baisse de limite bloque les nouveaux départs sans prétendre avoir arrêté les processus excédentaires. L’actualisation est manuelle.

Le processus actif vérifie son contrôle au plus toutes les cinq secondes dans sa boucle d’annulation. Une réponse perdue demande son arrêt. Les appels de contrôle et de nettoyage restent liés à la session/appareil/profil d’origine après pause ou changement de permissions ; ils ne permettent aucune nouvelle réservation ni publication. Une révocation complète de l’appareil bloque aussi ce canal et conserve l’incertitude pour examen humain.

Une erreur de quota ou d’authentification observée suspend les nouveaux départs du propriétaire jusqu’à reprise humaine explicite. La catégorie `quota_or_rate_limit` provient du diagnostic borné du client ; ce n’est pas une mesure d’utilisation. Le panneau affiche les pourcentages et heures de reset comme indisponibles, sans les inventer. Aucun changement de fournisseur ou de compte n’est effectué.

L’observation n’est appliquée qu’une fois par tour. Une confirmation de nettoyage ultérieure conserve sa raison historique et ne réactive pas une pause que le propriétaire a déjà levée.

## Reprise après une réponse perdue

```sh
python3 -m blackoutburst_agents runtime --journal /chemin/prive/runtime list
python3 -m blackoutburst_agents runtime --journal /chemin/prive/runtime \
  status --operation identifiant-local
```

Ces commandes lisent uniquement les métadonnées locales, sans connexion ni contenu du modèle. Les IDs servent à retrouver l’exécution exacte.

Si la fin a été enregistrée localement mais son accusé distant a été perdu :

```sh
python3 -m blackoutburst_agents runtime --journal /chemin/prive/runtime \
  reconcile --operation identifiant-local --state /chemin/prive/sessions-codex \
  --connection-file /chemin/prive/connexion.json
```

Cette commande prend le verrou du travail, retrouve son tour terminal exact et son résultat privé, puis rapproche le reçu du board. Elle ne réserve, n’active ni ne relance aucun modèle. Elle ne réenregistre pas le profil. Un autre journal de sessions, une autre connexion ou une preuve de nettoyage contradictoire sont refusés.

La portée persistée choisit le canal : ticket historique ou message exact. Un
message utilise sa connexion et son guard dédiés, sans créer de ticket. Pour le
résultat modèle exact d’une opération [développement](DEVELOPMENT_CLI.md), ajouter
`--development-operation UUID_DU_WORKJOURNAL` ; son UUID de work et son alias privé
doivent correspondre. Cette option est refusée pour une portée message avant
connexion. Elle ne rapproche pas les outils `local_broker`, ne relit pas leurs
sources et ne permet aucune nouvelle exécution après révocation.

Un démarrage dont l’accusé est perdu avant confirmation locale, ou un processus dont le nettoyage reste inconnu, conserve sa place. Il n’existe pas de bouton de libération forcée dans cette version. Résoudre séparément les effets et les journaux worker/file ; rapprocher la capacité ne déclare jamais le ticket réussi.

## Données et validation

Le serveur conserve uniquement des identifiants opaques, modèle/fournisseur, états, versions, dates et catégories de diagnostic. Il ne reçoit ni prompt, réponse, chemin, PID, session fournisseur ou clé. Le panneau d’un propriétaire n’expose pas les exécutions privées de l’autre.

Les tests couvrent plusieurs propriétaires, profils et appareils, pertes de réponses, cache, quota, changement de profil, révocation, annulation et nettoyage incertain. L’interface est vérifiée dans Firefox sur ordinateur et mobile avec deux comptes synthétiques. Aucun agent permanent ni prise automatique de travail en production n’est activé par cette livraison.
