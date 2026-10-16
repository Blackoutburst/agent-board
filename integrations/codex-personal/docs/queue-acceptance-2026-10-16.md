# Recette intégrée de la file — 16 octobre 2026

Cette recette utilise le vrai Board et SQLite dans des répertoires temporaires,
avec quatre profils, appareils, sessions, espaces de travail et journaux
indépendants, répartis entre deux propriétaires. `QueueWorker`, `BoardWorker`,
`PersonalRunner` et `RuntimeGuard` sont les composants réels ; seul le transport
du modèle est simulé. Aucun abonnement, MCP public, SSH ou listener de production
n’est utilisé. Le [reçu](queue-acceptance-2026-10-16.json) décrit les observations.

Trois scénarios intégrés complètent les tests séparés de sélection et Runtime :

1. Les quatre profils tentent de sélectionner l’unique ticket prêt en parallèle.
   Les réponses de sélection et la réponse de publication du gagnant sont
   perdues volontairement. Les reçus durables permettent le rapprochement : un
   seul tour simulé, une seule exécution Runtime et une seule publication. Les
   relances des quatre journaux ne sélectionnent ni n’exécutent à nouveau.
2. Chaque propriétaire exécute explicitement ses deux profils à la suite ; les
   deux propriétaires se recouvrent pendant leurs premiers tours. Les quatre
   tickets affectés sont distincts. Avant chaque premier effet simulé, le ticket
   expose le propriétaire, le profil, la session et la réservation, et Runtime
   montre l’exécution active. Dépendances non satisfaites, compétence absente,
   statut bloqué et projet hors portée restent sans réservation. Une tentative
   explicite vers le projet interdit ne déclenche aucun modèle ni sélection de
   remplacement. Les quatre rejeux sont servis du cache sans nouvel effet.
3. Deux tours restent actifs, un par propriétaire. Les deux autres profils
   sélectionnent leurs propres tickets mais sont refusés par Runtime avant le
   modèle. Les propriétaires peuvent travailler en parallèle, tandis que la
   limite de chaque propriétaire reste à un. Une nouvelle clé ne contourne pas
   l’opération locale restée en revue.

La sérialisation du deuxième scénario est imposée par le contrôleur de test.
La file actuelle ne transforme pas une saturation Runtime en attente automatique :
l’opération s’arrête en revue et conserve sa réservation de ticket pour un
rapprochement explicite. Le troisième scénario vérifie ce refus et ne prétend
pas démontrer une reprise autonome après libération de la capacité.

Les connexions personnelles réelles ont leurs propres preuves. Elles ne
transforment pas cette recette à modèles simulés en pilote réel de file via
abonnement. Le réveil distant, l’activation permanente, les baux longs et la
supervision des processus restent des validations séparées.

```sh
PYTHONPATH=integrations/codex-personal python -m pytest -q \
  integrations/codex-personal/tests/test_queue_multi_owner_acceptance.py \
  integrations/codex-personal/tests/test_queue_worker.py \
  integrations/codex-personal/tests/test_runtime_integration.py \
  tests/test_queue_selection.py tests/test_runtime.py
```
