# Connexion du fournisseur Codex personnel

La version 0.6 fournit un diagnostic, une preuve de connexion, un exécutant textuel durable, un lecteur de contexte et un coordinateur de ticket explicite compatibles avec le **MCP 2.0 du board**. Le mode question/réponse exige le board **2.0.3**. Elle conserve un seul registre d’identités : celui du board. Aucun profil ou fournisseur parallèle n’est créé dans ce module.

## Responsabilités actuelles

| Board 2.0 publié | Adaptateur personnel 0.6 |
|---|---|
| Connexion d’un appareil et identité persistante via `connect.py` / `register_agent` | Réutilise cette connexion et vérifie les identifiants retournés |
| Projets, tickets, conversations et permissions du propriétaire/appareil | Lit un ticket explicitement choisi et compose ses sources bornées |
| Réservations, baux, `get_work`, `claim_next_ticket` | `worker` réserve seulement un ticket explicitement choisi ; aucune sélection automatique |
| Délégation et demandes humaines | `worker run --human-owner` peut poser une question au destinataire fixé puis reprendre explicitement après sa réponse ; la délégation reste à raccorder |
| Activité et inbox | `events drain` lit l’activité ascendante d’un ticket et journalise ses lots ; il ne lit pas l’inbox et ne démarre pas de listener |
| Identité affichée et compétences | Conserve seulement les champs publics utiles à l’analyse |

Les fonctionnalités Strata/GLM annoncées par l’autre développeur ne sont pas implémentées ici. Le commit cité ne contient pas encore de registre de fournisseurs d’inférence ou de permissions distinctes par agent. Les accès effectivement vérifiés restent ceux du propriétaire et de l’appareil.

## Interfaces Python

```python
from blackoutburst_agents.subscription_probe import ProbeSettings, inspect_cli
from blackoutburst_agents.board_reader import BoardReader
from blackoutburst_agents.context import ContextScope, build_context
from blackoutburst_agents.context_cache import ContextCache

# Aucun appel de modèle :
diagnostic = inspect_cli(ProbeSettings(executable="/chemin/absolu/codex"))
reader = BoardReader.from_connection_file(connection_path, session_id=session_id)
registration = reader.register()  # autonomous=False, sans lecture de l’inbox
snapshot = build_context(reader.call, scope=ContextScope(owner, agent, project, ticket),
                         registration=registration)
with ContextCache(context_directory) as cache:
    receipt = cache.save(snapshot)
```

`BoardReader.call` n’accepte que `get_ticket` et `get_thread`. Il injecte la session choisie, refuse les redirections et borne les réponses à 2 Mio. Les erreurs sont des codes fixes sans corps distant ni identifiants de connexion. Le fichier de connexion conserve le format du `connect.py` commun ; aucun parcours de pairing n’est dupliqué.

`build_context` retourne le schéma déterministe `blackoutburst-ticket-context-v1` : `scope`, `sources`, `data`, `omissions`, `trust` et `fingerprint`. Les champs sélectionnés sont hachés avant leur éventuelle troncature. Les demandes humaines et messages restent non fiables, même lorsqu’ils mentionnent une autorisation. Les URLs, pièces jointes et chemins ne sont jamais suivis automatiquement.

`ContextCache` conserve dix révisions privées par portée, avec une empreinte inspectable et un effacement explicite. `verified_context` relit les sources, vérifie la portée locale et refuse tout changement avant un tour. Le [guide du contexte](CONTEXT.md) documente la pagination, les limites, le contrôle de fraîcheur et la distinction entre cache de sources et mémoire Codex.

## Exécution personnelle

`RunStore(state_dir)` fixe les libellés owner/agent/project/ticket, le dossier, le client et le modèle. `PersonalRunner(store).run(work_id, request_id, prompt)` traite une entrée explicite ou retrouve son résultat déjà enregistré. Son résultat contient `work_id`, `turn_id`, `cached` et `result` ; **`result.text` est privé et non fiable**. `public_result` produit un statut sans texte.

Le transport vérifie la connexion ChatGPT à chaque lancement et enregistre l’identifiant de session avant de traiter la réponse. Il reprend uniquement cette session. Clés réutilisées avec un autre contenu, sessions croisées, exécutions concurrentes et reprise ambiguë après coupure sont refusées. Le [guide de l’exécutant](RUNNER.md) précise l’annulation et la résolution humaine.

Un contexte MCP inspecté peut accompagner `runner turn`. Le contrôle porte sur la lecture et l’analyse textuelle. Il ne réserve pas le ticket, ne vérifie pas un droit de déploiement et ne permet pas d’exécuter les commandes trouvées dans les messages. L’état local d’un work ne constitue pas une identité authentifiée.

La sonde `run_subscription_probe(settings)` reste disponible pour un test explicite minimal. Diagnostic et sonde refusent le mode clé API ; aucun repli payant ou autre fournisseur n’est choisi automatiquement. Les états de quota, connexion, timeout, protocole ou nettoyage incertain sont rapportés par des codes bornés. Une preuve locale ne valide pas l’accès d’un second propriétaire ni un service mutualisé hébergeant ses connexions.

## Ticket explicitement choisi

`BoardWorker(journal, run_store, client).run(...)` utilise les identifiants réels du board, un journal privé à six champs de portée et la connexion personnelle épinglée. `WorkerBoardClient` limite ses mutations à la réservation et au résultat du ticket ; `BoardReader` reste en lecture seule. Le coordinateur ajoute le résultat et sa preuve, publie `review` ou `blocked`, puis libère sa réservation. Il ne choisit ni ticket, ni nouveau modèle, ni outil. Le [guide worker](WORKER.md) décrit les tentatives identiques, les interruptions et les limites de ce premier parcours.

L’option Python `human_owner_id` fixe le destinataire d’une éventuelle question. Elle exige `HumanWorkerClient`, qui ajoute seulement `lookup_human_request` et `request_human_input` avec `kind=question`. La configuration est immuable pendant toute l’opération. Le journal conserve l’intention avant l’envoi, la demande confirmée et la réponse ; il ne déclenche aucune attente permanente. Une invocation identique reprend le même work et la même session, avec une clé distincte pour le second tour. La réponse humaine et le nouveau contexte restent séparés de l’instruction locale.

## Raccordements restant à réaliser

- Étendre le traitement textuel explicite aux outils de développement autorisés et aux baux longs, sans affaiblir la réconciliation des effets après coupure.
- Étendre le drain ponctuel des événements d’un ticket aux réveils distants et aux autres sources autorisées. `read_inbox` avance son curseur à la lecture ; ce n’est pas un accusé de traitement réussi.
- Relier personnalisation, modèles accessibles et éventuels replis aux profils du board, sans copier de cache d’authentification entre propriétaires.
- Ajouter la sélection de tâches, les outils de développement et la publication uniquement dans le périmètre explicitement autorisé, avec contrôle avant chaque effet.
- Valider le second propriétaire et le mode d’hébergement avant l’activation d’un service partagé.

Le module ne démarre ni service ni boucle d’attente. Aucun modèle ne tourne pour attendre des messages.
