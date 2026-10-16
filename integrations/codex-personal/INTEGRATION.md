# Contrat de l’adaptateur Codex personnel

Référence vérifiée : board **2.9.0**, schéma **11**, adaptateur **0.19.0**. Le contrat public des champs, permissions, erreurs, migrations et outils MCP se trouve dans [CONTRACT.md](../../CONTRACT.md). Cette page définit la frontière entre ce serveur et l’exécutant personnel ; elle ne crée aucun registre d’agents ou fournisseur parallèle.

Le dépôt commun est `Blackoutburst/agent-board`. Les changements Markdown et pièces jointes de Blackout sont conservés. Les développements de cette orchestration sont dans ce dépôt, jamais dans Touhou6. Les versions anciennes de clients restent bornées à leurs outils ; un outil obligatoire absent entraîne un refus, sans repli vers une exécution non gérée.

## Identités et responsabilités

| Board | Adaptateur personnel |
|---|---|
| Comptes propriétaires, appareils appariés, profils durables et liaisons explicites | Réutilise le fichier de connexion privé du parcours commun ; vérifie propriétaire, appareil, profil et version |
| Projets, tickets, conversations, fichiers et droits effectifs | Compose seulement les sources autorisées de la portée choisie |
| Attribution, réservations et baux des tickets | Réserve avant le travail, vérifie la durée du bail et rapproche la même opération après interruption |
| Activité ascendante et demandes humaines | Journalise les événements ; pose une question au destinataire configuré et reprend la même session après sa réponse |
| Limites de concurrence, pause et contrôle des exécutions | En mode géré, réserve Runtime avant chaque tour, coopère à l’arrêt et ne libère la capacité qu’après nettoyage confirmé |
| Préférences des profils et observations d’usage | Applique une politique locale explicite de modèles accessibles, épinglée par tour ; aucun basculement de compte ou d’API |
| Interface des agents, projets, tickets et conversations | Commandes locales explicites et hôte au premier plan, désactivé à la création |

Une identité de profil persiste entre chats ; la session d’exécution reste distincte et fixée dans les journaux. Un nom affiché ou une déclaration du modèle ne constitue jamais une identité ni un droit. Les deux propriétaires partagent les projets autorisés, pas leurs connexions personnelles ou contextes privés. Le serveur conserve uniquement la connexion au board, jamais les identifiants du fournisseur de modèle.

Les préférences de fournisseur des profils ne prouvent ni accès ni connexion. Cet adaptateur utilise le Codex personnel connecté à ChatGPT. Il n’implémente aucun transport Strata/GLM et n’étend pas ses droits à partir d’un champ de préférence.

## Sources et contexte

Depuis Board 2.8, les réponses MCP sont compactes par défaut. `BoardReader`
et `EventBoardClient` ajoutent `view="full"` après validation stricte des
arguments pour préserver le texte source et l'activité attendus par les journaux.
Les aperçus ne sont jamais scellés comme source complète. Le contrat des pages,
chunks `get_text` et modes compatibles est dans [MCP-READS](../../docs/MCP-READS.md).

`BoardReader` enregistre une présence avec `autonomous=false`, sans lire l’inbox, puis limite ses lectures à `get_ticket` et `get_thread`. Il injecte la session choisie, refuse les redirections et borne les réponses. `build_context` vérifie la portée owner/agent/project/ticket, sélectionne profil, brief, critères, progression, dépendances, demandes humaines et messages récents, puis produit `blackoutburst-ticket-context-v1`.

Les sources sont identifiées et hachées avant troncature. `ContextCache` conserve dix révisions privées par portée ; inspection et effacement sont explicites. `verified_context` relit les sources autorisées et refuse une empreinte modifiée avant un nouveau tour. Le [guide du contexte](CONTEXT.md) décrit les bornes et les omissions. La [mémoire de décisions](MEMORY.md) permet un pin explicite d’un message humain exact, relu par `get_message`, sans parcourir tout l’historique. Elle sépare les portées, borne les résumés et invalide les anciennes sessions après correction ou reset ; aucune recherche sémantique automatique n’est ajoutée. RunStore passe au format privé 2 : arrêter les anciens coordinateurs avant sa migration locale.

Les messages, résultats, URLs et fichiers sont des données non fiables. Ils ne remplacent ni les instructions locales ni les autorisations. Le contexte ne suit pas automatiquement une URL ou un chemin et ne transmet pas l’historique privé d’un autre propriétaire.

## Exécution et effets durables

`RunStore` fixe la portée, le dossier, le client et le modèle. `PersonalRunner` exécute ou rapproche une entrée avec une clé stable, enregistre la session avant d’accepter ses résultats et reprend uniquement cette session. Une clé réutilisée avec un autre contenu, une session croisée ou une exécution ambiguë est refusée. Le texte du résultat reste privé ; les reçus publics contiennent des métadonnées expurgées.

`BoardWorker` compose ce mécanisme avec la réservation du ticket, le contexte frais et un `WorkJournal` de portée exacte. Le worker ordinaire livre en `review` ou `blocked`, avec résultat et preuve, puis libère sa réservation. Le [contrat worker](WORKER.md) distingue le nettoyage du processus, la libération Runtime et la publication du ticket : aucun de ces effets ne prouve les deux autres.

`HumanWorkerClient` ajoute la recherche et la création idempotente d’une demande `question`. Le destinataire est fixé localement. La réponse humaine reprend le même work avec une clé de second tour distincte ; une réponse doublée ne lance pas un troisième tour. Le parcours réel question/réponse et ses reçus sont décrits dans le guide worker et dans `docs/`.

La capacité Runtime est commune aux profils et appareils du propriétaire. Runtime est obligatoire pour l’hôte, optionnel pour les commandes historiques de runner/worker ; il ne couvre pas les clients qui n’y participent pas. Une erreur de quota ou d’authentification rapportée par une exécution participante met le propriétaire en pause ; une observation de pourcentage ou de remise à zéro ne le réactive jamais. Les lectures d’usage sont facultatives et ne lancent aucun modèle. Voir [RUNTIME.md](RUNTIME.md), [MODELS.md](MODELS.md) et [USAGE.md](USAGE.md).

Les anciens claims après changement de profil se rapprochent via `reconcile_profile_claim` sous une identité fraîche du même profil/appareil, avec preuve d’inspection et seulement après expiration. Le serveur conserve attentes, résultats et fichiers et refuse les exécutions au nettoyage incertain. Cette opération ne reprend aucun ancien modèle.

## Sélection et réveil

`queue` sélectionne ponctuellement un seul ticket éligible, avec intention durable et recherche de la sélection après perte de réponse. Il respecte attribution, compétences, ressources, pauses et droits ; une nouvelle clé ne permet pas de contourner un travail ouvert. Ce parcours est distinct d’un listener permanent.

`events drain` collecte les pages ascendantes de l’activité d’un ticket. `host` réutilise ce drain pour les tickets explicitement configurés : création désactivée, activation d’un budget fini, puis démarrage séparé au premier plan. Son intention préalloue l’UUID exact du worker enfant. Il n’adopte pas un travail manuel indépendant, même après une panne d’écriture. Voir [EVENTS.md](EVENTS.md), [QUEUE.md](QUEUE.md) et [HOST.md](HOST.md).

Le mode hôte `schema_version: 2` répond aux humains adressant le profil dans des conversations explicitement choisies, y compris générales, via le [flux confirmé](../../docs/MESSAGE-FEED.md). Il conserve les sources exactes de son contexte, les revalide avant modèle et publication, puis acquitte seulement une livraison traitée durablement. Le [guide messages](MESSAGE_HOST.md) décrit le budget fini, Runtime obligatoire et les reprises idempotentes. Le mode 1 demeure celui des activités de tickets. Aucun mode n’installe un service ; le verrou protège un compte OS sur une machine et ne constitue pas une location distribuée. `read_inbox` conserve son avancement historique à la lecture ; il n’est pas une preuve de traitement réussi. Le pilote distant reste séparé.

## Développement et fonctions restantes

Le noyau `DevelopmentBroker` de 0.13 est une bibliothèque **désactivée par défaut**, séparée du worker textuel. Il borne la lecture et le remplacement de fichiers existants dans un checkout isolé, exige des autorisations fraîches et journalise ses intentions avant effet. Les tests nommés refusent un backend non qualifié. Il n’expose encore aucun outil au modèle et ne lance aucun shell. [DEVELOPMENT.md](DEVELOPMENT.md) décrit ses limites, notamment l’usage exclusif du checkout et le rapprochement humain des stages incertains.

Le [backend supervisé](SUPERVISED_TESTS.md) de 0.14 fournit une qualification locale Mac sur des scénarios synthétiques, dont un crash du contrôleur ; il est opt-in et reste séparé des workers. Le [relevé Linux](LINUX_FEASIBILITY.md) garde le backend refusé : les écritures dans l’overlay et le nettoyage de la namespace demandent un contrat distinct. La [politique explicite du planificateur](PLANNER_POLICY.md) qualifie un dossier Mac vide en lecture seule ; le [contrôleur local](DEVELOPMENT_PLANNER.md) réserve Runtime séparément pour le modèle et chaque outil. Le [raccordement CLI/MCP explicite](DEVELOPMENT_CLI.md) prépare un plan puis réserve le ticket choisi avant exécution, revalide les droits et le contexte, et ne publie en revue que sur commande séparée. Il reste opt-in ; les droits vides refusent avant tout démarrage. Une opération possède son propre UUID et dialogue fournisseur. Les reprises Runtime distinguent tickets historiques, messages et développement ; une opération de développement ne peut pas nettoyer une portée message.

Restent à raccorder et à valider : transport de développement, délégation avec contrôle de la famille et recette distante avec plusieurs agents par propriétaire. Les preuves personnelles locales ne valent pas qualification d’un service mutualisé. Installation, mise à jour, observation de quotas et redémarrage du board n’activent aucun listener ou hôte personnel.
