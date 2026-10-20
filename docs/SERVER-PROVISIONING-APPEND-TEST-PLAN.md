# Ticket46 — validation du backend A et limites

La source backend `966085c301cb7628ba0e4a243efea59cf624caca` a été testée avec de vrais Board/SQLite temporaires et des transports ASGI/MCP en mémoire. Aucune CLI fournisseur, connexion personnelle, action serveur ou exécution de modèle n’a été utilisée.

- **84 cas auteur** passent dans `tests/test_server_provisioning.py` et `tests/test_server_provisioning_api.py`.
- Les **2 contrepreuves originales** d’historique/marqueur passent après correction : sélection totale86cas,11,65s.
- La revue indépendante finale a rejoué **12 preuves adverses**, plus le **vrai lecteur Board15** refusant16 sans mutation. Ses14 cas auteur ciblés sont un sous-ensemble, pas un nouveau total.
- **117 tests de compatibilité** contrôles/API/catalogue passaient sur le parent dac5df84 ; deux anciennes fixtures de schéma étaient exclues, leur adaptation appartenant à la composition finale. Aucune suite complète de la future release n’est revendiquée ici.

| Frontière vérifiée | Épreuve exécutée |
| --- | --- |
| Autorité | Grant humain séparé ; agent, autre propriétaire/device et absence de grant refusés |
| Pins et préférences | Snapshot profil calculée serveur, null pour modèle vide, version/fence/hash exacts, édition ou retrait de binding refusés |
| Entrées REST | JSON brut à clés exactes, doublons/NaN/types/Unicode/bornes refusés, CSRF/origine/session et absence d’écho de marqueurs sensibles |
| Vrai Stop | Observation seule et Stop non terminé insuffisants ; commande humaine complète requise |
| Courses | Deux ordres déterministes Start/demande, puis Restart/demande indépendant ; annulations concurrentes même clé et clés distinctes |
| Replays | Réponse perdue après COMMIT demande/annulation ; même corps/clé sans duplication ; ancienne autorité retirée ne permet pas le replay positif |
| Contournements | Anciens reçus Start/ready/publish/replace et publication vide refusés sous barrière ; réassociation bloquée |
| Contrôle existant | Client SDK0.30.2 réel vers MCP local ; poll fermé et Stop/ACK/cleanup disponibles après retrait de droits |
| Conservation | Snapshots des données hors write-set, historique32, catalogue15/16, pagination bornée ; aucune remise de budget/Runtime fabriqué |
| Postconditions | Altérations après audit de barrière/corps/grant/instance/reçu/audit ; historique non courant, recul user_version, DDL et application_id refusés/rollback |
| Schéma | Triggers main/TEMP, vues/tables masquantes ; migration15→16 atomique, panne DDL et ancienne lecture refusée |
| Refus permanents | Migration avec une vraie réservation Runtime synthétique non finie et un refus d’admission durable ; rows et preuve conservées |

Les tests n’utilisent pas la présence de ready, un modèle demandé ou un ACK comme preuve de connexion. Les fautes d’audit simulent des écritures imprévues dans un contrôleur coopératif ; elles ne qualifient pas une résistance à du Python hostile disposant de l’accès SQLite arbitraire. Les pages respectent la borne de réponse ; ce lot n’est pas une campagne exhaustive de fuzzing de toutes les combinaisons de tailles ou corruptions.

Les résultats backend ne valident pas à eux seuls le parcours Firefox de la composition UI, le service réellement installé, deux propriétaires connectés, les quotas ou les modèles accessibles. La suite complète et les adaptations de fixtures/métadonnées sont vérifiées séparément sur le SHA final de release.

B reste hors validation : aucun append ni provenance par cible appliqué, aucune nouvelle configuration Host, aucun pairage/login fournisseur, aucun test natif Linux, aucun nettoyage de Runtime réel préexistant. Des tests de commit/reprise inter-DB, de fichiers privés/FD, de conservation des feeds et d’attestations seront nécessaires lors de leurs implémentations. Les demandes de métadonnées A restent inertes et ne sont jamais adoptées comme preuves ou ordres B.
