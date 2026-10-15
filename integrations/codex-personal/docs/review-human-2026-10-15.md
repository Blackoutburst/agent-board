# Revue du parcours question/réponse — 15 octobre 2026

L’adaptateur 0.6 ajoute une question d’information à un destinataire fixé localement, puis une reprise explicite dans la même session Codex. Ce parcours exige le board 2.0.3 et ne démarre aucun listener.

Deux agents distincts ont relu le coordinateur et le client/CLI. Le code serveur de consultation a été relu séparément par l’intégrateur ; le contrôle de version ajouté par l’intégrateur a été relu par un autre agent.

## Défauts corrigés pendant la revue

- Un drapeau de consultation sur l’ancien outil de création pouvait être ignoré par un ancien serveur MCP. L’outil séparé `lookup_human_request` échoue alors sans création ; aucun repli ne simule la lecture par une mutation.
- Une modification humaine pouvait arriver après le dernier contrôle de contexte, avant la création de la question. `expected_version` est désormais vérifié dans la transaction de création. Le scénario reproduit ne crée aucune question et ne libère aucune réservation ; la tentative suivante demande une revue du contexte modifié.
- Le schéma MCP entier permissif convertissait une chaîne de version avant la validation du cœur. Le type strict partagé `Version` refuse chaînes, flottants et booléens sans effet.

## Validation

- 308 tests de l’adaptateur passent, dont 26 tests dédiés au parcours humain avec le vrai Board et des bases temporaires.
- 128 tests serveur passent, dont le transport MCP officiel et la lecture sans effet sous SQLite `query_only`.
- Les tests couvrent réponse de création perdue, réponse humaine reçue avant réconciliation, interruption après le second tour, même session de modèle, question unique, propriétaire ciblé, retrait de droits, réaffectation, réservation concurrente, dépendance, bail expiré, limites UTF-8 et publication perdue.
- Le journal passe atomiquement en version 2 au premier handoff ; un échec SQL annule également cette version. Les journaux uniquement textuels restent en version 1.

L’inférence de ces tests est simulée. Aucun échange humain réel ni service autonome continu n’est certifié par cette revue. Les preuves antérieures de connexion personnelle, reprise Codex et publication d’un ticket restent disponibles dans ce dossier.
