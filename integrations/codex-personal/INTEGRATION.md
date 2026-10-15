# Connexion du fournisseur Codex personnel

La version 0.2 expose un diagnostic, une preuve de connexion et un exécutant de tours texte avec état local durable. Elle est indépendante du registre des agents et des autres fournisseurs du board. Les propositions de raccordement ci-dessous ne décrivent pas une API du board déjà déployée.

## Interface disponible

```python
from blackoutburst_agents.subscription_probe import (
    ProbeSettings, inspect_cli, run_subscription_probe,
)

settings = ProbeSettings(executable="/chemin/absolu/codex")
diagnostic = inspect_cli(settings)  # aucune inférence
# Seulement après une demande explicite de test :
proof = run_subscription_probe(settings)  # un tour minimal, sans nouvelle tentative
```

Ces fonctions sont synchrones. Elles doivent tourner dans un processus de l’exécutant personnel, hors de la boucle HTTP du board. Le diagnostic renvoie `status=ready` uniquement si la CLI reconnaît sa connexion ChatGPT. La preuve renvoie `status=passed` uniquement après une sortie correcte, un événement de fin et le message exact attendu. Les autres résultats ont `status=blocked` et un `reason` borné. Les paramètres invalides lèvent `ValueError`; la CLI les convertit en code de sortie 2.

Le résultat JSON de la sonde a `schema_version=1`. Il ne contient ni jeton, ni sortie brute, ni contenu du tour. Le reçu CLI l’enveloppe dans `blackoutburst-personal-subscription-proof-v1`. Une preuve réussie est une observation ponctuelle, pas une garantie de disponibilité future. `requested_model=null` signifie que le modèle par défaut du client a été demandé ; son identifiant effectif n’a pas été relevé.

Principaux états à présenter :

| `reason` | Conséquence pour le board |
|---|---|
| `ready` | Diagnostic reconnu, le tour n’a pas encore été testé |
| `verified` | Tour de test terminé et réponse vérifiée |
| `login_required`, `authentication_failed` | Le propriétaire doit utiliser le parcours officiel de connexion |
| `api_key_auth_not_allowed` | Ce connecteur accepte uniquement le mode ChatGPT |
| `quota_or_rate_limit` | Afficher l’indisponibilité ; ne pas changer de facturation |
| `timeout`, `output_limit` | Test interrompu ; aucun succès établi |
| `cleanup_failed` | Arrêt des descendants non confirmé ; vérifier le processus avant un nouvel essai |
| `protocol_error`, `unrecognized_version`, `unrecognized_login_status` | Version ou format non reconnu ; mettre à jour l’intégration avant de relancer |
| Autres valeurs | Échec explicite ; conserver le code sans afficher les sorties privées |

## Exécutant local disponible

`RunStore(state_dir)` crée l’état privé. `create_work` fixe les libellés owner/agent/project/ticket, le dossier, le client et le modèle. `PersonalRunner(store).run(work_id, request_id, prompt)` exécute une entrée explicite ou retrouve son résultat déjà enregistré. Le retour contient `work_id`, `turn_id`, `cached` et `result` ; **`result.text` est privé et non fiable**. `public_result` produit la projection de statut dépourvue de ce texte.

Le transport `execute_turn` vérifie la connexion à chaque lancement et enregistre l’identifiant de session avant de traiter la réponse. Les nouvelles entrées reprennent seulement cette session. Les changements de contenu sous une même clé, les sessions croisées et les exécutions simultanées du même contexte sont refusés.

`request_cancel` enregistre une demande d’arrêt, `status` expose les métadonnées, et `resolve_interruption` permet une résolution humaine après vérification des processus. Une coupure ambiguë n’autorise aucune nouvelle tentative automatique. Le [guide de l’exécutant](RUNNER.md) précise les commandes et les limites. Le schéma local SQLite est en version 1 ; une version future inconnue est refusée.

Ces libellés de contexte ne constituent pas des identités authentifiées. Le futur appelant doit vérifier les droits et l’attribution du ticket avant d’utiliser cet exécutant. L’état privé et le cache d’authentification restent dans l’installation du propriétaire.

## Répartition proposée

Le board conserve une seule définition des agents : identité, personnalisation, projets autorisés, fournisseur choisi, attribution et historique des tickets. Ce module ne crée aucun profil, aucune conversation et aucun listener.

Un futur exécutant Codex reçoit une référence de connexion appartenant au propriétaire de l’agent. L’authentification demeure dans son environnement personnel. Le board reçoit seulement les capacités et les résultats expurgés ; il n’a pas besoin de récupérer le cache d’authentification Codex. Plusieurs agents d’un même propriétaire partagent les limites de sa connexion.

Avant d’exécuter de vrais tickets, le contrat commun doit encore fixer :

1. Les identifiants `owner`, `agent`, `run`, `attempt` et les permissions vérifiées avant chaque effet.
2. La réservation exclusive du ticket et sa reprise après une coupure.
3. La livraison durable des événements, leur déduplication et les réponses humaines attendues.
4. La projection des états locaux, de l’annulation et des erreurs de quota dans le board, ainsi que les checkpoints des effets externes.
5. Les modèles effectivement accessibles et les éventuels replis explicitement choisis par le propriétaire.

Ce raccordement reste à implémenter. Le test personnel ne tranche pas l’éligibilité d’un service central hébergeant les connexions de plusieurs utilisateurs. Aucun basculement vers un autre fournisseur ou une API payante n’est effectué par ce connecteur.
