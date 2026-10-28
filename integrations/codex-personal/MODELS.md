# Choix explicite et traçable du modèle

L’adaptateur 0.10 peut choisir un modèle selon une classe fixée par l’opérateur : `tri`, `simple`, `difficult` ou `review`. Il utilise une politique locale du propriétaire, avec une liste autorisée et une observation datée pour chaque modèle. Aucun catalogue intégré ne garantit l’accès d’un abonnement. Un refus ou un quota arrête le parcours sans changer de compte, de fournisseur ou de modèle.

La [documentation officielle](https://learn.chatgpt.com/docs/models) distingue les modèles adaptés aux tâches ciblées et aux travaux complexes, mais leur disponibilité dépend du compte et du client. Une observation locale `verified` signifie qu’un essai a été constaté sur cette installation ; elle ne garantit ni le prochain appel ni son quota.

## Politique privée

Le fichier JSON doit appartenir à l’utilisateur et avoir le mode `0600`. Les doublons, champs inconnus, liens et fichiers trop volumineux sont refusés. Le fournisseur pris en charge ici est `codex_chatgpt`. Le propriétaire et la connexion doivent correspondre au profil réellement enregistré avant toute réservation.

L’identifiant de connexion est calculé par `model_policy.connection_identifier(scope, registration)` : `codex-connection-` suivi du SHA256 du JSON canonique `{board_endpoint, owner_id, agent_id, device_id}`. Il ne contient aucune clé. Une autre session sur le même binding conserve cet identifiant ; un autre appareil ou profil exige sa propre observation. Ce hash n’atteste pas cryptographiquement l’identité du compte ChatGPT.

Structure d’exemple, volontairement sans modèle vérifié :

```json
{
  "schema_version": 1,
  "policy_id": "personal-models",
  "version": 1,
  "owner_id": "example-owner",
  "connection_id": "example-binding",
  "provider": "codex_chatgpt",
  "proof_max_age_seconds": 86400,
  "models": {
    "example-model": {
      "reasoning_efforts": [],
      "availability": {
        "status": "unknown",
        "observed_at": null,
        "reference": null,
        "owner_id": "example-owner",
        "connection_id": "example-binding",
        "provider": "codex_chatgpt",
        "model": "example-model"
      }
    }
  },
  "routes": {
    "tri": {"model": "example-model", "reasoning_effort": null},
    "simple": {"model": "example-model", "reasoning_effort": null},
    "difficult": {"model": "example-model", "reasoning_effort": null},
    "review": {"model": "example-model", "reasoning_effort": null}
  }
}
```

Après un essai explicite avec `probe --model`, le propriétaire peut enregistrer son observation avec `status: "verified"`, l’horodatage Unix réel et une référence opaque à son reçu. `unknown`, `unavailable`, une observation future ou trop ancienne empêchent le départ. Dans ce parcours de routage personnel, les efforts de raisonnement restent `null` : l'adaptateur ne transmet pas ces préférences et refuse une politique qui les imposerait. L'option explicite du [planner de développement](DEVELOPMENT_PLANNER.md), disponible depuis 0.40, est un contrat séparé ; elle n'active pas les préférences de ce routeur.

## Utilisation et reprise

Ajouter ces options à `worker run`, `events drain` ou `queue run` :

```sh
--model-policy /chemin/prive/modeles.json --task-class review
```

Avec une politique, `--model` est un override explicite qui doit aussi être autorisé et observé. Sinon la préférence du profil est utilisée si elle est définie, puis la route de la classe. Une préférence non autorisée est refusée ; le programme ne la remplace pas silencieusement. Sans politique, le comportement explicite antérieur est conservé.

La décision contient le modèle, sa classe, un motif fixe, la version du profil, l’observation et les hashes de politique et de contexte. Elle est conservée avant la création de la session modèle. Une reprise après coupure ou réponse humaine valide cette même décision ; elle n’effectue pas de nouveau choix. Le contexte initial de décision reste distinct du contexte courant que le worker revalide. Changer de politique, de profil, de modèle ou retirer l’option à la reprise provoque un examen.

Les quatre classes, les overrides, refus d’accès déclarés, changements de binding/profil et reprises sont couverts avec le vrai board temporaire et des modèles simulés. L’interface des profils conserve ses préférences ; l’édition complète de cette politique reste locale.
