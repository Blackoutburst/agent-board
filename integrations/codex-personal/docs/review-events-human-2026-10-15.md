# Revue de la reprise humaine depuis les événements

L’adaptateur 0.8 raccorde un lot explicitement exécuté à une question d’information au destinataire fixé localement, puis reprend la même opération et session. Il conserve un journal événements v2 et ne démarre aucun listener.

La revue indépendante a couvert le client composé, la validation CLI, le pin de profil, les preuves de liaison, la migration et l’absorption atomique des réponses. Elle a reproduit puis fait corriger un abandon après perte de l’accusé de création : sans identifiant de question, le lot ne peut plus être clôturé silencieusement. Le rapprochement explicite vérifie d’abord la portée locale puis effectue uniquement l’enregistrement et la consultation de la demande exacte. Une absence ponctuelle n’autorise aucune clôture.

Une réponse serveur de 80 000 octets peut être rapprochée après abandon sans être injectée au modèle ; la reprise normale conserve sa limite de 12 000 octets. Les réponses étrangères, changements de profil, réaffectations, révocations, interruptions et arrivées tardives ne relancent pas automatiquement l’exécution.

Validation locale : 352 tests de l’adaptateur passent, dont 16 tests adversariaux indépendants et 98 tests events. Les scénarios utilisent de vrais Board/SQLite temporaires et des modèles/transports simulés. Cette recette ne démontre pas encore une question/réponse avec deux personnes réelles, un réveil distant, une sélection automatique de tâches ou des outils de développement.
