# Ticket 46 — contrat A : demande durable et barrière inactive

**Proposition à revoir avant implémentation.** Base `400438a150054117d2a28fa646c45fd21d6b78b0`, arbre `dc626c1c590b132ae25199ea1bdefe9f5b59b4b8`. Ce lot ne modifie actuellement aucun produit, schéma ou numéro de version.

## Périmètre fermé

A ajoute un grant humain distinct et une demande durable de préparation d’**une** cible messages, sur un hôte existant de 1 à 15 cibles. La demande bloque Start/Restart jusqu’à son annulation explicite. **A n’a aucun consommateur manager, ACK d’effet, append, fichier privé, pairage, login, nouvelle instance, processus ou modèle.** La préparation effective B sera un contrat ultérieur ; A ne doit pas être silencieusement converti en ordre exécutable par un futur binaire.

À la base, `provider_onboarding.py` et le wizard créent seulement un profil/préférences ; `host_append_stage.py` produit seulement un stage privé inerte. `server_provisioning.py` n’existe pas : c’est le nouveau module proposé. `replace_server_catalog` garde son contrat source-only, à identités constantes. Aucun élargissement de cette API, des outils MCP ou de `ServerControlClient.call()`.

## Autorité et formes proposées

La seule capacité annoncée est `metadata_request_only_v1`, `apply_implemented=false`, `login_implemented=false`. Aucun template d’append n’est annoncé utilisable. Le grant est **désactivé par défaut**, humain REST uniquement, lié à owner authentifié + hôte + device exact déjà possédé/lié, et au purpose fermé `metadata_request_only_v1`. Le grant de contrôle serveur actuel, les droits de travail, un ticket ou un message ne l’accordent pas. Il n’autorise pas de provisioning effectif ou futur login. Les profils doivent être owned/actifs, de version exacte et déjà explicitement liés au device ; A ne crée pas ce binding. Toutes les transitions/replays revérifient owner/device/hôte/profil et grant actuels avant d’adopter un reçu historique. Un grant révoqué n’autorise plus de demande ; l’humain propriétaire conserve la possibilité d’annuler la demande existante.

Routes **proposées**, session humaine/origine vérifiées et `no-store` :

| Route | Corps exact / résultat |
| --- | --- |
| `PUT /api/server-hosts/{host}/provisioning-grant` | `schema, expected_host_version, expected_grant_version, enabled, idempotency_key` ; purpose/device issus du contrat et du vrai hôte |
| `POST /api/server-hosts/{host}/provisioning-requests` | `schema, expected_host_version, expected_catalog_version, expected_instances_sha256, grant_version, profile_id, profile_version, profile_sha256, provider_id, requested_model, idempotency_key` |
| `GET …/provisioning-requests` et `GET …/provisioning-requests/{id}` | Résumés propriétaires fermés ; liste paginée <=16 |
| `POST …/provisioning-requests/{id}/cancel` | `schema, expected_revision, idempotency_key` |

Schémas littéraux séparés : `server-provision-draft-grant-v1`, `server-provision-draft-request-v1`, `server-provision-draft-cancel-v1`. Les réponses ne transportent pas les clés d’idempotence. owner/device/host viennent des identités authentifiées, jamais du corps. Fournisseur/modèle doivent reproduire exactement les préférences de la version du profil, sans attestation. `requested_model` est null ou 1..200 caractères/800 octets sans contrôles. Identifiants : bornes opaques actuelles 1..200 caractères/800 octets. SHA : 64 hex minuscules. Entiers exacts, jamais bool ; versions positives, grant absent attendu 0, catalogue 1..2^63-2. JSON ASCII canonique trié, nombres finis, doublons/extra keys/coercitions refusés ; corps <=8 Kio, réponse <=16 Kio. Pas de path/shell/UID/env/template libre/endpoint/challenge/credential/instruction dans ces formes.

Réponse grant fermée : `schema, owner_id, host_id, device_id, purpose, version, enabled` ; aucun token. Le digest de demande lie contrat, corps complet y compris key, identités authentifiées et purpose. Une clé est unique pour **owner+purpose+opération**, pas hôte seulement : la réutiliser sur un autre host/corps est un conflit. Le même key chez un autre owner est indépendant. Pas de normalisation tardive qui changerait le premier corps.

## Transaction, conservation et compatibilité

Une transaction sérialisée commune aux mutations du contrôle serveur vérifie grant courant, CAS host/catalogue, `catalog_fence` v1 exact de **toutes** les instances et profil/pins, puis crée demande + barrière + reçu + audit. Chaque instance existante exige dernier **vrai Stop humain complet**, desired/observed stopped, cleanup confirmé et aucune commande queued/accepted. Une observation stopped, un ACK ou un ancien terminal Start ne suffit pas. Une seule demande active par hôte ; une place conceptuelle réservée (jamais un budget modèle), plafond 16 et <=32 demandes retenues. Aucune suppression/GC implicite.

Pendant la barrière, vérifier au même point transactionnel : commande Start/Restart, ACK d’un ancien Start, rapport positif ready/busy/succès, publish normal, replace source, nouvelle demande et réassociation d’hôte. Un reçu historique n’est pas l’autorité courante. Course Start/demande : un seul gagne ; si Start gagne, demande refusée ; si demande gagne, aucune commande Start ne naît. Stop et rapports de refus/nettoyage restent autorisés. Un Stop nouveau ne rebase jamais le fence de la demande ; son historique est conservé.

**Compat SDK0.30.2 :** `poll_instance_commands` reste byte-shape compatible, sans nouveau champ/enum. Pendant la barrière, `can_start=false`, `control_reason=authorization_changed` : l’autorisation de lancer est effectivement suspendue. Le motif précis est seulement dans la nouvelle projection REST humaine. Pas de `provisioning_pending` ajouté à l’ancien wire fermé. Le contrôleur legacy peut continuer Stop/cleanup ; la barrière ne prétend pas arrêter un processus ou révoquer rétroactivement un lease. **Ne pas déployer A avant clôture de la récupération 47**, dont le coordinateur source exige actuellement `can_start=true`.

A ne modifie aucune instance/commande/Runtime ni état privé. Le write-set est grant/demande/barrière/reçus/audit seulement. Revocation du grant/host/device bloque toute nouvelle autorité, mais ne supprime pas la demande ni sa barrière. Une erreur ou un timeout ne lève rien. Après audit/reçu, postconditions : demande unique/corps canonique exact/barrière correspondante, rows du contrôle serveur et pins conservés ; refus/rollback si side effect inattendu. Schémas/triggers/views/main-temp shadows du write-set sont fermés et validés avant transactions sensibles ; la migration sera atomique, avec un marqueur nouveau qu’un ancien Board refuse avant toute mutation. Aucun numéro n’est fixé par ce document.

## États et reprise

A admet seulement `requested` puis `cancelled`, revision monotone, terminal immuable. Les états prepared/commit/complete/login n’existent pas dans A. Une annulation CAS humaine ferme atomiquement la demande et la barrière ; c’est sûr précisément parce qu’aucun consommateur/effet n’existe. Cette clôture ne prouve jamais cleanup/non-lancement d’un autre travail. Si un effet/ACK inconnu existe, la forme n’est plus A : refus, pas annulation par hypothèse.

Après retour perdu : relecture/retry **explicite** même corps/key ; une seule demande/place et aucune remise de budget. Pas de nouveau key/message/worker automatique. Un lookup absent seul ne prouve rien. La révocation/expiration/reboot n’annule pas une demande. Un état incohérent refuse sans réparation/adoption. Le terminal n’est pas un nouveau droit à agir. Upgrade futur B exige nouveau grant/contrat, jamais migration des demandes A en ordres B.

Projection fermée proposée : `schema, request_id, request_sha256, owner_id, host_id, device_id, revision, state, reason, barrier_held, reserved_slots, profile_id, profile_version, provider_id, requested_model, apply_implemented, connection_status, model_catalog_status, model_access_status, can_cancel`. reason : `requested|cancelled_by_owner` ; les changements de droits sont projetés par `can_cancel`, booléen calculé pour la session humaine, sans raison libre. `apply_implemented=false` et trois attestations `not_attested`. Aucun texte CLI/exception/secret. Le wizard distingue profil/souhait, grant, demande/barrière, puis instance ; il n’annonce pas « prêt ». Timeout garde key/corps ; 409 impose reload ; 400/404/422 permettent correction explicite/nouvelle key après relecture.

## B différé : prérequis uniquement

- `launch_catalog` v1 lie chaque launchspec au digest **global** de config : ajouter une cible changerait tous les anciens hashes. Ne pas modifier v1 ni mentir dans `control.hash`. B devra prouver un binding de provenance versionné par cible, avec origine vérifiée, vrai hash global du candidat et consommateurs/pins MessageBackend explicitement compatibles ; sinon refus.
- Slot Board personnel appairé explicitement, template fixe/piné et identité exacte ; un SHA connection_file/stage inerte n’en est pas la preuve. Pas de copie d’auth ou de register implicite.
- Journal/effets privés sous owner lock et chemins/FD tenus ; CAS/reprise inter-DB, aucune adoption de partiels ni faux cleanup. Aucun work/turn/Runtime/message outbox pending/delivery non ACKée avant commit.
- Commit ajoute une cible disabled/budget0 ; anciens IDs/targets/launchspec/budgets/cursors/histoires intacts, barrière tenue jusqu’aux deux commits exacts et binding final. `replace_server_catalog` reste même-identités/source-only.
- Provider/login/attestations/recette réelle deux owners restent séparés. Ni ancien Runtime465, ni verrou v5, ni qualification Linux ne changent ; les demandes A ne deviennent jamais des ordres B.
