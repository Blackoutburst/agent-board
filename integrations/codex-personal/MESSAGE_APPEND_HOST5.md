# Host5 — contrat local d'append messages

Source actuelle : Board 2.17.0/schema17 et SDK0.31.0. Le parcours privé Host5 est
raccordé au vrai worker `managed-run` sous acquisition propriétaire ; il effectue
seulement les RPC fermés et les phases finies de B. Aucune installation ne lance
de service ou de modèle. Les prérequis et limites sont décrits dans
[SERVER-MESSAGES-APPEND.md](../../docs/SERVER-MESSAGES-APPEND.md).

Base historique de ce lot : main d95697d60b81e8b79be27e039606bb34b39b2f20,
SDK0.30.5/Host4/Message3. Le module pur message_launch_bindings.py et L1 restent inchangés.

## Autorité et acquisitions

Toutes les mutations exigent la même vraie acquisition HostJournal.owner_lock, son token vivant, PID/thread exacts, la configuration messages2 et un gestionnaire déjà lié. Aucun bool, JSON ou état inspecté ne reconstitue cette acquisition. Les RPC sont exécutés par le futur coordinateur hors transaction ; le raccord recheck est une garde locale readonly concrète (source/UID/slots/roster d'enfants) sous les locks existants, pas une preuve de droits sérialisée. Les reçus Board attendus sont fermés et liés à une opération unique ; leur contenu n'est pas une authentification indépendante. Aucun caller effectif n'est livré avant le raccord du coordinateur.

## API privée et wire fermés

- locked_message_append_children(host, messages, runs, runtime, *, operation_id=None) : handle réel non sérialisable sous owner/work locks ; operation_id permet seulement de relire le C0 du même intent en reprise froide.
- propose_messages_append(candidate, *, operation_id, expected_host_revision, expected_configuration_sha256, expected_catalog_sha256, expected_release_sha256, children, anchor_operation_id=None) : plan fermé privé depuis C0 vivant ; aucune mutation. Au premier append, anchor_operation_id est un UUID neuf distinct d'operation_id ; aux suivants il vaut None.
- begin_messages_append(plan, barrier_receipt, *, children, recheck) : Host5 + tables strictes + ancre actuelle C0 + intention atomiques, phase intent/revision1.
- commit_messages_append(intent_id, expected_revision, precommit_receipt, *, children, recheck) : C1 réel + bindings validés + nouvelle cible inactive/budget0 + vrai catalogue effectif en une transaction, phase local_committed/revision2.
- bind_messages_append_instance(intent_id, expected_revision, append_receipt, *, children, recheck) : lie uniquement le nouvel instance_id exact de la réponse Board, phase instance_bound/revision3.
- complete_messages_append(intent_id, expected_revision, completion_receipt, *, children, recheck) : clôture locale après preuve de clôture Board exact, phase complete/revision4.
- messages_append_record(operation_id=...) : lecture bornée strictement validée, jamais reprise automatique.
- require_messages_append_recovery(operation_id) : inspection de startup par le vrai owner, uniquement pour l'intent Host5 pending exact et un hôte déjà inactif ; les gardes source/47/48 restent strictes. Aucun effet positif ni admission n'est autorisé par ce garde.
- effective_message_catalog(host, expected_release_sha256) : v1 intact si Host<=4 ; Host5 utilise uniquement les bindings entièrement validés, sans fallback C1.

Le plan contient owner/endpoint/host_id/host_instance, R installé attendu, opération UUID, vrai hashC0/C1, catalogue précédent et suivant, bindings courant et candidat, roster consommé et empreinte du write-set préservé. Les chemins et secrets restent privés ; le Board reçoit uniquement les SHA et les entrées publiques fermées.

Le proof wire possède exactement 23 champs : schema, operation_id, request_id, request_sha256, owner_id, host_id, device_id, grant_version, request_revision, expected_host_version, expected_catalog_version, expected_instances_sha256, source_release_sha256, old_configuration_sha256, new_configuration_sha256, old_catalog_sha256, new_catalog_sha256, bindings_sha256, children_roster_sha256, phase, instance_id, catalog_version, instances_sha256. Schema=server-messages-append-proof-v1 ; prepared/revision2 puis precommit/revision3 ont les trois derniers champs à None ; committed/revision4 puis complete/revision5 les fixent. Chaque frontière conserve exactement tous les pins et l'identité initiale ; committed/complete conservent aussi l'instance et le fence. Le Board ne certifie aucun de ces pins privés.

## État durable et préservation

Host5 créé seulement au premier begin explicite, jamais en ouverture/inspection ; une ancienne DB ou un stage A n'est pas adopté. L'ancre kind=anchor décrit le prédécesseur actuel avec son UUID distinct, même avec plusieurs anciennes cibles. Append+1 seulement, préfixe et globals canoniques exacts ; le dernier target seul est nouveau, start_at=now. Le plan attend R depuis la source protégée, pas depuis un document libre. Le compteur global et les anciennes activations, budgets/générations, reçus, abonnements, feed/readers/checkpoints restent intacts. control.configuration est réellement C1 et control.hash=digest(C1). Aucun host.json réécrit.

Schéma SQL/JSON exact et borné, pas de triggers main/TEMP, vues, ombres ou index inconnus ; taille contrôlée avant parse. Plan UTF8 limité à 16 Mio ; l'historique utilise un manifeste de SHA par ligne bornée, sans réencoder la totalité des anciens plans. Vérifier marker/appid/schema et total_changes autour de chaque mutation/callback. Le callback source/slot est limité en lecture seule sur les quatre connexions SQLite ; une tentative d'écriture est refusée avant l'effet, même si le callback avale l'exception. Toutes les anciennes opérations et lignes historiques restent présentes. Un état terminal est immuable ; un old plan ne peut écraser ou rembourser une transition. Réponse perdue/reprise froide : même opération/corps/phase exacts ou refus, jamais adoption par simple absence. Le coordinateur doit persister ce plan privé inerte avant prepare RPC sous la clé messages-append-prepare-<plan_sha256> ; il ne peut pas recalculer ou adopter un nouveau snapshot après perte de réponse.

## Gates et compatibilité

Host<=4 conserve son comportement existant. Le marqueur5 fait refuser l'ancien lecteur avant DDL. Pendant intent/local_committed/instance_bound, aucun Start/Restart, publication ou effet positif ; Stop reste possible, mais un Stop supplémentaire qui change la génération invalide le CAS de transition et ne relance rien. Le sélecteur effectif est utilisé aux chemins de startup/publication. Configure/prepare/stage/recovery historique et CatalogSourceTransition v1/v2 refusent Host5 ; les terminaux historiques restent lisibles et validés, sans lancer un ancien écrivain sous le nouveau schéma.

Le rapprochement des enfants doit traiter le roster réel complet, incluant sealed47, executed_disposed48 et un completed normal, sans élargir le guard historique limité à deux enfants ni convertir leur preuve. Un work open après un completed est normal ; l'état du turn et du Runtime, publication et ACK doivent être vérifiés. Les helpers purs/recettes n'inventent aucune autorité Board ni cleanup réel.

## Tests de ce lot

Composition SQLite temporaire Host4 puis Host5, ancre multi-cibles et append1/C2 ; anciennes rows/feed/readers/payloads exacts et budget non rechargé ; trois enfants historiques de genres distincts et refus d'orphelins/pending ; vrais owner lock/FD/token/PID/thread ; reprises et CAS ; corruptions rehashées/triggers/marker/écriture étrangère ; gates v1 et Host4 actifs inchangés ; sélecteur v1/Host5 et aucune reconstruction de données manquantes. Pas de relance des tests pure8f65 sans motif, ni de qualification ou effet réel.

