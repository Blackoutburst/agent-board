# Travailleurs serveur et équipe hiérarchique

L’objectif est une équipe qui traite le board de bout en bout : un manager
découpe et attribue les tickets, des codeurs livrent, des reviewers indépendants
vérifient et des rédacteurs documentent. Chaque rôle correspond à un profil,
une configuration de modèle et des autorisations explicites. Un profil créé,
un thread local ou une réservation ne prouvent pas qu’un modèle travaille.

## État de la source 0.32.0

| Élément | État |
| --- | --- |
| Pool durable et capacité locale de 1 à 8 | Implémentés, reprise sans relancement implicite |
| Quatre parcours de développement parallèles | Testés avec Board/Runtime réels temporaires et modèles simulés |
| Préparation Git, sélection et qualifications Linux par job | API explicite, sans dispatch automatique |
| Commit scellé et PR brouillon | API explicite, avec garde de tests et reprise des effets ambigus |
| Revue indépendante | Worker et raccord Linux explicites |
| Unité serveur du pool | À raccorder et à valider dans son environnement définitif |
| Quatre modèles réels simultanés | À valider avec les comptes propriétaires |
| Attribution hiérarchique et rôle rédacteur | À implémenter et à valider de bout en bout |

Le premier lot de développement produit des résultats privés. Ni la fin d’un
thread ni le nettoyage d’une réservation ne publient une PR ou ne terminent un
ticket. Les étapes de livraison, de revue et de publication ont leurs propres
intentions durables et leurs contrôles d’autorité.

## Validation Linux datée du 21 octobre 2026

Le commit `cdf0aba2d719965367fd69b9a9ba2c8a5360c385` a passé les neuf
contrôles synthétiques du backend et les cinq contrôles de la politique Linux,
avec la fermeture exacte de 64 sources. Sous Python 3.10.12, UID 1000 et noyau
5.15.0-198-generic, quatre tests supplémentaires ont passé avec les véritables
observations `/proc` des verrous Linux. Le scénario complet observe quatre
Runtime corrélés simultanés, huit tours de texte simulés et seize réservations
terminées avec nettoyage confirmé. Aucune connexion de modèle ni publication
GitHub réelle n’a eu lieu dans ces tests.

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
