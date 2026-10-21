# Ticket 46 — demandes de préparation : comportement livré en source

Ce lot A enregistre une **demande de métadonnées** et suspend les démarrages de l’hôte jusqu’à son annulation humaine. Il ne prépare pas automatiquement une cible et ne connecte aucun fournisseur. Le backend relu est `966085c301cb7628ba0e4a243efea59cf624caca` ; sa migration Board15→16 est atomique. Cette documentation ne constitue ni un déploiement ni une recette réelle à deux propriétaires.

## Périmètre et autorisations

`server_provisioning.py` fournit un grant humain distinct et des demandes `requested` / `cancelled`. Le purpose unique est `metadata_request_only_v1`. Le grant absent est présenté avec `version=0, enabled=false`. Le grant de contrôle serveur existant, les permissions de travail, un ticket ou une mention ne donnent pas ce nouveau droit. Seul le propriétaire humain authentifié peut l’accorder, demander la préparation et l’annuler.

Le grant est lié à l’hôte et à son device exact, déjà possédés ; il ne crée ni appareil ni rôle. Une nouvelle demande vise un profil actif du même propriétaire, de version exacte, déjà explicitement lié à ce device et jamais publié sur cet hôte, même en historique inactif. Sa préférence fournisseur doit être non vide ; son modèle peut être non renseigné. Les deux doivent correspondre exactement au corps reçu. Le grant peut être enregistré sur le catalogue existant ; la demande exige **1 à 15 cibles existantes, toutes de workflow messages**. Aucun profil, binding ou cible n’est créé par cette opération.

Avant une nouvelle demande ou son replay, le backend vérifie l’hôte/device vivants, le grant actuel et le profil/binding/pins actuels. L’activation du grant exige également l’hôte/device vivants. En revanche, les lectures propriétaires et l’annulation restent disponibles après retrait du grant, du binding, des droits de travail ou révocation du device/hôte ; une session humaine valide reste obligatoire. Un replay ancien ne réactive jamais un grant révoqué.

## API humaine fermée

Toutes les routes passent par la session humaine, les contrôles d’origine/CSRF pour les mutations et `Cache-Control: no-store`. Aucun outil MCP ou client manager n’a été ajouté.

| Route sous `/api/server-hosts/{host}` | Comportement |
| --- | --- |
| `GET /provisioning-grant` | Grant direct `server-provision-draft-grant-view-v1` : owner_id, host_id, device_id, purpose, version, enabled |
| `PUT /provisioning-grant` | Corps exact : schema, expected_host_version, expected_grant_version, enabled, idempotency_key |
| `GET /provisioning-context?profile_id=…` | Pins publics calculés serveur ; aucune autorité de mutation |
| `POST /provisioning-requests` | Corps exact : schema, expected_host_version, expected_catalog_version, expected_instances_sha256, grant_version, profile_id, profile_version, profile_sha256, provider_id, requested_model, idempotency_key |
| `GET /provisioning-requests?before=…&limit=16` | Liste `server-provision-draft-list-v1` : requests et next_before ; limite 1..16 |
| `GET /provisioning-requests/{id}` | Projection courante de la demande propriétaire |
| `POST /provisioning-requests/{id}/cancel` | Corps exact : schema, expected_revision, idempotency_key |

Les schémas de corps sont respectivement `server-provision-draft-grant-v1`, `server-provision-draft-request-v1`, `server-provision-draft-cancel-v1`. Aucun owner/device libre n’est accepté dans un corps. Champs inconnus, doublons JSON, coercitions et nombres non finis sont refusés. Les corps des nouvelles mutations sont bornés à8Kio ; les anciens contrôles gardent leur limite4Kio. Les réponses sont bornées à16Kio. Une page peut contenir moins de lignes que limit ; next_before est le dernier ID rendu, ou null. Identifiants : 1..200 caractères/800 octets sans contrôles ; SHA-256 : 64hex minuscules ; versions : entiers exacts positifs, grant absent attendu0, catalogue1..2^63-2.

Le contexte `server-provision-draft-context-v1` contient schema, owner_id, host_id, device_id, expected_host_version, expected_catalog_version, expected_instances_sha256, grant_version, profile_id, profile_version, profile_sha256, provider_id, requested_model. Le SHA profil porte sur la snapshot immuable de la version (JSON ASCII canonique trié) et le fence sur `catalog_fence` v1 : le navigateur n’a pas à reproduire les floats Python. Une préférence modèle stockée `''` est projetée en **null** ; toute chaîne non vide reste exacte (<=200 caractères/800 octets). La mutation compare le corps original, sans normalisation après idempotence. Le fournisseur doit être une préférence non vide, pas une preuve de connexion.

La projection `server-provision-draft-view-v1` contient exactement : schema, request_id, request_sha256, owner_id, host_id, device_id, revision, state, reason, barrier_held, reserved_slots, profile_id, profile_version, provider_id, requested_model, apply_implemented, connection_status, model_catalog_status, model_access_status, can_cancel. reason vaut `requested` ou `cancelled_by_owner`. can_cancel vaut true seulement pour requested et ne prétend pas que le grant est encore actif. apply_implemented vaut toujours false ; les trois attestations restent **not_attested**. Aucun challenge, credential, chemin privé, instruction ou sortie fournisseur n’est transporté.

## Barrière et conservation

La transaction sérialisée de création vérifie les CAS hôte/catalogue/fence/profil, le grant et le catalogue entier. Chaque instance doit avoir un **dernier vrai Stop humain complet** : desired/observed stopped, cleanup confirmé, commande actuelle exacte et terminée. Une observation stopped ou un ACK isolés ne suffisent pas. Une seule demande active et une place conceptuelle sont réservées par hôte ; ce n’est pas un budget de modèle. Les32 historiques maximum sont conservés ; une nouvelle demande au-delà est refusée, sans effacement.

La barrière est contrôlée avant les reçus historiques de Start/Restart, ACK de Start, rapports positifs, publications de catalogue et réassociation d’hôte. Les remplacements de source et secondes demandes sont également refusés. Les deux ordres de course Start/demande n’ont qu’un gagnant. Stop et rapports de refus/nettoyage restent utilisables. Un Stop supplémentaire ne réécrit ni le fence d’origine ni son historique.

Le wire du SDK0.30.2 reste fermé et inchangé. Sur un hôte non révoqué, la barrière produit can_start=false / control_reason=authorization_changed : l’autorisation de démarrer est suspendue. Elle n’atteste ni arrêt de processus, ni cleanup, ni révocation rétroactive d’un lease. Le motif spécifique est dans la projection humaine. Terminer toute récupération explicite de catalogue en cours avant d’activer ce workflow : son coordinateur attend can_start=true, que la barrière suspend.

Le write-set ne touche que grant/demande/barrière/reçu/audit. Après le vrai audit, le backend exige exactement3 écritures DML pour un grant ou4 pour une demande/annulation, et conserve schema_version/application_id ; user_version reste au marqueur courant avant et après :16 pour la source historique A,17 pour la source actuelle qui ajoute B. Les postconditions recoupent aussi corps, barrière, instance/commande courantes, reçu et audit. Triggers, vues ou masquages main/temp du write-set, schémas inattendus et états incohérents sont refusés. Une faute constatée avant commit provoque le rollback ; aucun nettoyage Runtime ou historique n’est déduit de ce contrôle. Un ancien Board15 refuse le marqueur16.

## Annulation, replay et limites

Une demande reste requested, revision1, jusqu’à son annulation explicite : cancelled, revision2, barrière levée atomiquement. Aucun manager ne consomme ces demandes ; il n’existe ni ACK d’effet ni apply. L’annulation ne prétend donc rien sur un autre travail ou un autre processus. Révocation, reboot ou silence ne l’annulent pas automatiquement ; un état inconnu est refusé sans réparation.

Les clés sont liées à owner+purpose+opération et au premier corps complet, avec host/device inclus dans son digest. Réutiliser une clé pour un autre corps/hôte provoque un conflit ; les namespaces propriétaires restent distincts. Après réponse perdue, seul un retry **explicite du même corps et de la même clé** peut retrouver le reçu. Aucun budget n’est rendu, aucun worker/message/Start n’est créé. Un reçu POST rejoué peut être historique : l’interface doit relire les GET courants. Le backend ne met pas en place de boucle de retry dans le navigateur. Un terminal est immuable.

Les demandes A **ne seront pas promues en ordres exécutables du futur B**. Une préparation effective exigera un autre contrat/grant, un slot Board personnel explicitement appairé, des chemins/outils privés vérifiés et un commit Host/Board repris par CAS. `host_append_stage` reste inerte. `launch_catalog` v1 dépend du digest global : un append changerait toutes les anciennes launchspec ; B doit résoudre cette provenance sans mentir dans control.hash ni modifier gratuitement les anciens hashes. `replace_server_catalog` garde sa portée même-identités/source-only. Login, modèles attestés, qualification Linux et recette réelle à deux propriétaires restent séparés. Les essais de développement et leurs nettoyages restent indépendants.
