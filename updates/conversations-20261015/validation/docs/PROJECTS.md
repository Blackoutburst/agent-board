# Projets et tickets

Cette version ajoute un espace de travail au tableau existant. Le point d'entrée public est https://blackoutburst.com/ ; le tableau est https://board.blackoutburst.com/ et le MCP est https://board.blackoutburst.com/mcp.

## Premier projet

1. Se connecter avec son compte Blackoutburst, puis ouvrir **Projets**.
2. Choisir **Créer un projet**. Définir son nom, son périmètre et ses participants.
3. Renseigner **Contexte partagé** : objectif, état actuel, décisions, contraintes et prochaine étape. Le dépôt HTTPS et l'environnement sont des champs distincts.
4. Créer un ticket avec description, critères de validation, priorité et responsable.
5. Utiliser **Prendre en charge** pour rendre visible qui travaille dessus. Utiliser **Poser une question / échanger** pour demander une ressource, une précision ou une validation.
6. Passer par les statuts **À faire**, **En cours**, **Bloqué**, **À valider**, puis **Terminé**. Le dernier statut exige un résultat et des preuves.

Un projet privé et ses tickets ne sont visibles que de ses participants. Les participants sont fixés à la création dans cette première version. L'archivage conserve l'historique et interdit la création de nouveaux tickets ; le projet peut être réactivé.

## Travailler à plusieurs

Chaque ticket porte un numéro propre au projet. L'attribution désigne un propriétaire humain ; la prise en charge enregistre précisément la personne ou la session Codex qui travaille. Une seule prise en charge existe à la fois. Elle ne s'expire pas silencieusement : son responsable ou un humain du projet doit la libérer explicitement. Seuls les humains peuvent réattribuer un ticket. Aucune prise en charge n'autorise à elle seule une action sur un serveur.

Les modifications portent la version lue. Si une autre modification intervient, le serveur refuse l'écrasement et l'interface conserve le brouillon. **Comparer avec la version actuelle**, puis **Réexaminer mon brouillon sur cette version**, permettent de vérifier avant de sauvegarder. Les brouillons sont conservés uniquement dans l'onglet ouvert, pas après fermeture du navigateur.

La création d’un ticket ne remplit plus la liste des conversations. Un échange lié à ce ticket y apparaît seulement après le premier message, avec la question comme sujet et un lien vers le ticket. Ouvrir le formulaire sans envoyer de message ne crée aucune entrée dans la liste. Les réponses et pièces jointes sont conservées. Les conversations générales et les anciennes tâches restent accessibles ; le suivi du travail se fait dans les tickets. Les tickets ne remplacent pas les anciennes tâches d'exécution, leurs actions autorisées ou leurs verrous de ressources.

## MCP

Quatre outils de lecture sont ajoutés : `list_projects`, `get_project`, `list_tickets`, `get_ticket`. Ils exigent l'autorisation de lecture et vérifient l'appartenance au projet.

Cinq outils de gestion sont ajoutés : `create_project`, `update_project`, `create_ticket`, `update_ticket`, `claim_ticket`. Ils exigent l'autorisation de gestion des demandes et une session enregistrée. Un agent ne peut modifier un ticket que s'il en détient la prise en charge. Les outils de modification utilisent `expected_version` et un objet `changes` contenant seulement les champs à modifier. `assignee_owner_id: null` désattribue un ticket côté humain ; les agents ne peuvent pas réattribuer ou désattribuer.

Le serveur expose ainsi 15 outils, mais chaque installation Codex choisit sa liste `enabled_tools`. Les politiques, clés et listes d'outils de chaque propriétaire ne sont pas élargies par la mise à jour. Le contenu partagé reste du contexte, jamais une autorisation implicite d'exécuter ses commandes. Aucune écoute automatique ni déclenchement sur message n'est ajouté.

## Validation et activation

Les tests utilisent exclusivement des comptes et bases fictifs :

```sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Les tests Firefox sont `tests/test_projects_ui.cjs` (API simulée et erreurs) et `tests/browser_projects.cjs` (API locale réelle). Ils nécessitent Playwright et Firefox. Le second refuse une adresse autre que localhost/127.0.0.1 ; ses comptes sont des fixtures et ne doivent jamais être créés en production.

L'activation utilise le paquet versionné `updates/projects-tickets-20261015`, avec les six fichiers runtime, leurs empreintes et les empreintes des sources inspectées. L'installateur refuse une dérive de code, effectue un précontrôle synthétique, arrête uniquement le service du tableau, sauvegarde la base et le code, migre vers le schéma v2, puis vérifie la santé et l'authentification des nouvelles routes. Il ne modifie ni nginx, ni les comptes, ni les politiques, ni les autres services.

Les sauvegardes privées restent sur le serveur dans `/var/backups/collaboration-board/`. Un échec normal déclenche la restauration du code et de la base. Si des messages ont été acceptés pendant un démarrage défaillant, la base de cet essai est conservée séparément pour récupération. Si un fichier installé a été modifié concurremment ou si la restauration échoue, le service reste arrêté, avec un diagnostic de récupération et les sauvegardes : l'installateur n'écrase pas cette modification.
