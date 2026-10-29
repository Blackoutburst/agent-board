# Reprise du travail par cycles — source en préparation

Le ticket #60 ajoute une tâche logique durable, séparée des générations de
travail. La tâche n'a aucun plafond de cycles ou de durée totale. Chaque
génération garde les plafonds approuvés du planificateur, du broker, des tests
et de l'unité serveur. Cette source est une préparation distincte de la
version 0.44.2 ; elle n'est pas encore validée par un parcours réel ni déployée.

Un cycle propre conserve le checkout, les actions confirmées, leurs reçus et
la filiation Git dans un checkpoint privé. Le contrôleur termine et vérifie
le nettoyage Runtime, puis libère normalement sa propre réservation de ticket.
La génération suivante reçoit un nouveau journal, une nouvelle session et une
nouvelle admission. Les fichiers d'un cycle historique restent inchangés.

Si le fournisseur devient indisponible, le checkpoint et l'état de pause sont
conservés. Le contrôleur attend le backoff puis relit une observation fraîche
avant une nouvelle préparation. Il vérifie encore la disponibilité avant
l'admission. Aucun appel modèle périodique, changement de compte ou changement
de modèle ne sert à tester le retour du quota. Une disponibilité inconnue
ne vaut pas une autorisation.

Une action dont le résultat est incertain, un nettoyage non confirmé ou une
permission manquante exige une intervention. Une réponse humaine ne réécrit
pas les reçus, n'efface pas Stop et ne rend pas une opération incertaine
rejouable. L'état local `needs_you` doit rester distinct d'une demande humaine
confirmée par le board.

## Sélection explicite

Le nouveau fichier, propriétaire et privé comme les deux fichiers du
[workflow](CLOUD-WORKFLOW.md), lie chaque travail à une identité logique stable :

```json
{
  "schema": "linux-cloud-continuation-v1",
  "batch_file": "/PRIVATE_OWNER_ROOT/config/batch.json",
  "workflow_file": "/PRIVATE_OWNER_ROOT/config/workflow.json",
  "concurrency": 1,
  "provider_backoff_seconds": 30,
  "tasks": [{
    "name": "developer",
    "logical_task_id": "00000000-0000-4000-8000-000000000001",
    "checkpoint_after_actions": 2
  }]
}
```

Les chemins et l'UUID ci-dessus sont des exemples. Le lot doit utiliser le
schéma explicite v2. Les noms doivent correspondre exactement aux travaux du
lot et du workflow. Les plafonds sont recopiés du lot ; ce document ne permet
pas de les augmenter. Les identifiants, les trois fichiers, les outils, les
sources et les connexions restent vérifiés. Modifier cette sélection ne
reprend pas un historique dont les pins diffèrent.

## Installation et activation séparées

Le parcours est réservé à un paquet compatible 0.45 ou ultérieur, après revue
et installation de ce paquet. `continuation-plan` lit uniquement les trois
fichiers opérateur ; son résultat n'est pas une qualification d'exécution.

L'action `install-continuation` du gestionnaire requiert un paquet public
déjà installé et son reçu, ainsi que l'ancienne unité finie arrêtée et
désactivée. Elle ajoute un nouveau service et un nouveau timer désactivés,
puis recharge systemd. Elle ne lit aucun journal ou fichier de connexion
privé, ne crée pas d'état logique et ne lance aucun travail. Une installation
partielle est conservée pour examen, sans adoption automatique.

Le service de continuation garde les limites et le durcissement de l'unité
finie, avec `Restart=no`. Le timer est une activation humaine séparée et ne
rattrape pas les déclenchements manqués. Chaque passage est borné ; les
checkpoints durables relient les passages successifs. Stop, les baux, les
capacités Runtime partagées et les droits du board continuent de s'appliquer.

Le résultat final doit provenir des tests de la génération finale. La
publication rassemble les modifications depuis la base publique d'origine,
puis une revue distincte doit viser cette publication exacte. Une preuve de
code terminé ou un passage du service réussi ne signifie ni projet terminé,
ni revue approuvée, ni PR fusionnée.
