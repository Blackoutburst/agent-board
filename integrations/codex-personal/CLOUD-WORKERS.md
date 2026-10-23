# Travailleurs serveur et équipe hiérarchique

L’objectif est une équipe qui traite le board de bout en bout : un manager
découpe et attribue les tickets, des codeurs livrent, des reviewers indépendants
vérifient et des rédacteurs documentent. Chaque rôle correspond à un profil,
une configuration de modèle et des autorisations explicites. Un profil créé,
un thread local ou une réservation ne prouvent pas qu’un modèle travaille.

## État de la source 0.33.0

| Élément | État |
| --- | --- |
| Pool durable et capacité locale de 1 à 8 | Implémentés, reprise sans relancement implicite |
| Quatre parcours de développement parallèles | Testés avec Board/Runtime réels temporaires et modèles simulés |
| Préparation Git, sélection et qualifications Linux par job | API explicite, sans dispatch automatique |
| Commit scellé et PR brouillon | API explicite, avec garde de tests et reprise des effets ambigus |
| Résultat du pool vers livraison et passage en revue | Raccord explicite à un résultat terminé, sans relancer modèle ou tests |
| Parcours `run-workflow` | Un à quatre codeurs sélectionnés, PR brouillon et avis exact d’un reviewer distinct sur le même ticket ; intégration testée avec modèles et GitHub simulés |
| Revue indépendante | Driver Pool, préparation Linux explicite et publication durable de l’avis en statut `review` |
| Mode historique `run-batch`/schema v1 | Quatre résultats privés ; la fin du lot ne publie pas de PR |
| Unité serveur du pool | Installateur et entrée `run-workflow` implémentés ; nouvelle unité installée, arrêtée et désactivée au démarrage, qualification propriétaire à effectuer |
| Quatre modèles réels simultanés | À valider avec les comptes propriétaires |
| Rôles manager et rédacteur | Composants privés implémentés et testés, sans attribution ni dispatch des autres rôles |
| Contrôleur hiérarchique et attribution | À réaliser et à valider de bout en bout dans le ticket #52 |

`run-workflow` compose les étapes de développement, livraison et revue avec
leurs propres intentions durables et contrôles d’autorité. La fin du parcours
signifie que la PR brouillon et l’avis sont publiés, même si le reviewer demande
des corrections. Le ticket reste en revue et la PR reste sans fusion.

Le mode historique `run-batch`/schema v1 produit toujours quatre résultats
privés. Le schema v2 accepte un à quatre travaux ; utilisé avec `run-batch`,
il conserve cette sortie privée. Ni la fin d’un thread ni le nettoyage d’une
réservation ne constituent une publication.

Le [raccord de livraison](DEVELOPMENT_POOL_DELIVERY.md) traite un seul résultat
sélectionné et terminé : commit vérifié, PR brouillon, preuve sur le ticket,
puis retrait de l’attribution de l’auteur pour permettre une revue indépendante.
Le [contrôleur fini `run-workflow`](CLOUD-WORKFLOW.md) appelle ensuite le
[lifecycle du reviewer](docs/REVIEW-LIFECYCLE.md), qui publie l’avis exact et
conserve le SHA et le lien de PR sur le ticket. Les fichiers modifiables sont
des chemins existants choisis par l’opérateur, avec un vérificateur Python
nommé et épinglé. Le parcours ne fournit pas de shell arbitraire ou de création
de nouveaux fichiers par le modèle.

Les [composants manager et rédacteur](TEAM-ROLES.md) restent distincts de ce
contrôleur fini : plan borné au catalogue opérateur pour le manager, synthèse
de reçus historiques corrélés pour le rédacteur. Leur résultat est privé ; ils
ne distribuent pas encore les tâches de l’équipe.

Au 23 octobre 2026, la nouvelle unité du pool est installée, arrêtée et désactivée au démarrage. Aucun
modèle réel n’a validé ce nouveau parcours serveur. Les tests d’intégration
avec Board, Git, journaux et Runtime temporaires vérifient le raccord logiciel ;
leurs transports de modèle/GitHub et leurs qualifications Linux sont simulés.
L’authentification des propriétaires, les qualifications dans l’unité finale et
une recette réelle restent nécessaires avant de déclarer l’équipe active.

## Vérification Linux du 23 octobre 2026

La source `9f06c359bb7a56a18a562cef3f72ab3d3da0503a` ferme 84 modules et a passé
les neuf scénarios backend et les cinq sondes planner, avec nettoyage confirmé.
Cette observation reste une fixture synthétique UID1000 ; elle ne qualifie pas
le compte propriétaire ni son abonnement et n’a exécuté aucun modèle. Le paquet
SDK0.33 correspondant est installé avec son unité arrêtée et désactivée au démarrage.

## Validation Linux datée du 21 octobre 2026

Le commit `cdf0aba2d719965367fd69b9a9ba2c8a5360c385` a passé les neuf
contrôles synthétiques du backend et les cinq contrôles de la politique Linux,
avec la fermeture exacte de 64 sources. Sous Python 3.10.12, UID 1000 et noyau
5.15.0-198-generic, quatre tests supplémentaires ont passé avec les véritables
observations `/proc` des verrous Linux. Le scénario complet observe quatre
Runtime corrélés simultanés, huit tours de texte simulés et seize réservations
terminées avec nettoyage confirmé. Aucune connexion de modèle ni publication
GitHub réelle n’a eu lieu dans ces tests.

L’[observation publique datée](docs/linux-worker-fixtures-2026-10-21.json)
conserve ces reçus synthétiques et leurs limites. La régression complète du SDK
couvre 2 599 tests réussis : quatorze tests réseau ont nécessité localhost
autorisé et la fixture de fermeture des clients a été mise en conformité avec
les entrées de profil familial déjà exigées par le produit.

Le composé `f8cf6a775827728cd96e4aa556bc68799369eb63` ajoute le
[lanceur serveur fini](../../deploy/worker_pool/README.md). Ses 47 tests ont
passé sous Linux sans saut, puis le scénario à quatre profils distincts a passé
avec les véritables verrous `/proc`, seize réservations nettoyées et huit tours
simulés. Les neuf contrôles du backend et les cinq de la politique passent sur
sa fermeture de 65 sources. L’[observation datée](docs/linux-finite-pool-fixtures-2026-10-21.json)
conserve ces résultats ; ils ne qualifient pas la future unité propriétaire.

Ces observations datées ne qualifient pas une installation propriétaire. Chaque
job doit être préparé dans l’unité définitive, avec ses outils, sources,
permissions, compte, environnement et suite de tests épinglés. La capacité du
pool ne remplace pas celle du Runtime propriétaire. Une exécution historique
dont le nettoyage est inconnu conserve sa place ; les ressources indépendantes
restent utilisables si leurs propres admissions passent.

## Contrat de l’équipe à livrer

Le manager doit conserver l’attribution avant lancement et rapprocher les
effets après coupure. Un seul agent réserve un ticket à la fois. Le reviewer
doit être distinct de l’auteur et contrôler la livraison exacte ; un avis ne
doit pas autoriser une livraison ultérieure différente. Le rédacteur intervient
sur les résultats vérifiés. Les préférences de modèles restent explicites par
profil, avec budgets bornés et sans changement implicite de fournisseur.

L’interface doit distinguer les profils configurés, les travaux en attente,
les modèles réellement en cours, les demandes humaines et les blocages de
nettoyage. L’arrêt doit empêcher de nouvelles admissions et conserver les
preuves incomplètes. La validation finale doit couvrir les deux propriétaires,
les doublons de messages, la compétition pour les tickets, les reprises,
les échecs, le Stop et la consommation réelle de capacité.
