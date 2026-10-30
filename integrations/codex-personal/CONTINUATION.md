# Reprise du travail par cycles — adaptateur 0.45.3

Le ticket #60 ajoute une tâche logique durable, séparée des générations de
travail. La tâche n'a aucun plafond de cycles ou de durée totale. Chaque
génération garde les plafonds approuvés du planificateur, du broker, des tests
et de l'unité serveur. La source 0.45 compose les frontières de cycle,
l'indisponibilité fournisseur, l'agrégat final, la revue distincte et le service
borné. Elle n'a pas encore été validée par un parcours réel sur le serveur.
Le pilote fini 0.44.2 a réussi avec nettoyage confirmé le 29 octobre 2026 et sa
PR #74 a été fusionnée. Le premier pilote de continuation #62 a confirmé sa
lecture et son patch, puis s’est arrêté au contrôle du résultat du test Linux
avec `linux_result_boundary_invalid`. Le timer a été arrêté par le propriétaire ;
la génération et sa source restent conservées. La réconciliation normale a
ensuite confirmé le nettoyage des six réservations Runtime ; le test reste
non validé et le Core historique reste en `needs_you`. La recette à deux générations réelles, publication et revue reste
à valider. La version 0.45.1 sépare les contrôles locaux de Stop/identité/délai
des observations complètes aux frontières d’action ; les autorisations natives,
les captures SOURCE et la fenêtre de 1,5 seconde restent obligatoires.

Le diagnostic du pilote 0.45.1 a ensuite confirmé deux générations, le premier
checkpoint et sa retraite, puis un deuxième Worker `result_ready` avec un
planificateur `review_ready`. Le Core est resté en `needs_you` à la finalisation ;
les douze reçus Runtime locaux étaient terminés avec nettoyage confirmé, et
le contrôle serveur a permis la libération normale de la claim expirée. Le
timer est arrêté et ce Core historique reste conservé.

La version 0.45.2 prend en charge une dernière génération qui vérifie le travail
déjà sauvegardé sans ajouter de patch. Sa capture intermédiaire reste privée et
vérifie les fichiers et les tests ; le delta final publié reste calculé contre
la base publique d’origine et doit être non vide. Un scénario synthétique a
reproduit le refus antérieur après `read → test → final` ; le parcours serveur
complet avec publication et revue distincte reste à valider.

Le 30 octobre 2026, le pilote 0.45.2 a repris son checkpoint dans une seconde
session, validé la postimage attendue et publié la PR brouillon #77. La passe
s'est ensuite arrêtée après vingt minutes pendant la revue ; aucun avis final
ni nettoyage final du reviewer n'est encore confirmé. Ce résultat ne valide
pas le parcours complet et l'ancien essai n'est pas relancé.

La version 0.45.3 rend linéaire l'accumulation des octets dans la lecture
protégée des fichiers. Elle évite les copies répétées du préfixe des gros
exécutables, tout en gardant les mêmes octets retournés, limites, descripteurs,
observations d'identité et empreintes complètes. Les droits, contrôles de
source, délais et règles de reprise restent identiques. La mesure locale sur
un fichier synthétique de 64 Mio passe de 5,326 s à 0,030 s ; elle ne démontre
pas la cause exacte de l'arrêt serveur ni une validation réelle de cette version.

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
rejouable. L'état local `needs_you` reste distinct d'une demande humaine
confirmée par le board. Le raccord de visibilité peut poser une question fixe
sur les permissions ou les effets seulement avec le vrai Worker/Core vivant,
une claim propre encore courante, la source et le fence frais, et Runtime
positivement nettoyé. Il conserve une intention puis retrouve l'ACK sans
renvoyer une question incertaine. À défaut, l'avis fermé existant exige encore
le droit de réponse ; un refus reste localement en attente. Une pause propre,
le backoff et Stop ne déclenchent pas cette alarme. Les réponses dans Needs you
ne rouvrent pas le Core et ne constituent aucun grant d'exécution.

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

Les scripts standalone du manager et de l'entrée s'exécutent toujours avec
`-I -B`, après vérification des fichiers publics exacts, y compris
`pool_package.py`. Ne pas précréer le répertoire privé `continuations` : un état
partiel ou déjà présent ne devient pas une nouvelle préparation autorisée.

Le service de continuation garde les limites et le durcissement de l'unité
finie, avec `Restart=no`. Après configuration et lecture du plan, l'activation
humaine distincte `systemctl enable --now` du timer démarre les passages et les
active au boot. Le timer ne rattrape pas les déclenchements manqués. Chaque passage est borné ; les
checkpoints durables relient les passages successifs. Stop, les baux, les
capacités Runtime partagées et les droits du board continuent de s'appliquer.

Le résultat final doit provenir des tests de la génération finale. La
publication rassemble les modifications depuis la base publique d'origine,
puis une revue distincte doit viser cette publication exacte. Une preuve de
code terminé ou un passage du service réussi ne signifie ni projet terminé,
ni revue approuvée, ni PR fusionnée.
