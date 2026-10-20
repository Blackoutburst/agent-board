# Ticket 46 — épreuves source-only du contrat A

Plan seulement, base400438a. Conservation de toutes rows hors grant/demande/barrière/reçu/audit ; pas de compte réel ni modèle.

| Cas concret | Montage / attaque | Oracle |
| --- | --- | --- |
| Autorité distincte | Manager du contrôle serveur sans grant, agent demandant grant/request, autre device du même owner, ownerB lit/annule A | Refus, pas d’élévation ni fuite ; humain exact seulement |
| Grant CAS/idempotence | Grant absent attendu0, réponse perdue puis retry ; même key enabled/host/body changés | Une version, replay exact ou conflit ; pas de promotion device |
| Profil/pins | Profil étranger/inactif/non lié, édition de version/provider/model après lecture | Refus ; aucun binding ni choix modèle automatique |
| Formes bornées | Extra keys, bool/float versions, NaN/surrogates, duplicate JSON keys,201char/8193bytes, shell/path/token marqueurs | Refus avant métier, aucun écho secret dans erreur/audit |
| Stop réel | never-started observation stopped, Start rejeté, Stop queued/accepted, une instance review | Demande refusée ; seul dernier vrai Stop complet de chaque instance suffit |
| Fence exact | Changer séparément host/catalogue/fence/profileSHA ; cycle Start/Stop fini entre lecture et demande | Refus sans réserver place ; aucune rebase implicite |
| Courses sérialisées | Deux connexions, rendez-vous avant transaction, exécuter les deux ordres Start/Restart↔demande | Exactement un gagnant ; aucune commande Start après barrière |
| Tous contournements serveur | Barrière puis métier direct/REST/rawMCP : Start, ACK ancien Start, report ready/busy, ancien reçu, publish, replace, nouvelle liaison | Refus frais ; pas d’API future cachée ; Stop/report négatif fonctionnent |
| Compat wire legacy | Parser strict SDK0.30.2 réel sur poll sous barrière ; Stop+ACK+report réels | Aucune nouvelle key/enum ; authorization_changed/can_startfalse, pas de faux diagnostic cleanup |
| Retry/namespace/bornes | Timeout après commit request ; même key/body puis key autre host/corps ; même key ownerB ;15/16targets,32historiques | Une place/request, conflit attendu, owners isolés, aucun GC/remisebudget |
| Revocation/cancel | Revoke grant/host/device après request puis vieux retry ; cancel propriétaire encore authentifié ; deux cancels CAS concurrents | Aucun nouveau droit ; barrière non levée par silence ; un terminal cancelled exact |
| Annulation honnête | Cancel demandé après fake ACK/effet injecté inconnu ; JSON terminal ou schemaA malformé | Refus sans prétendre absence d’effet ; A ne consomme aucun ACK de manager |
| Postconditions | Triggers/side effects après insertion/audit/reçu : effacer barrière, altérer corps, créer Start, supprimer demande | Rollback complet, pas de réponse succès ; triggers/views/main-temp shadows refusés |
| Migration/reopen | Vraie ancienne DB avec rows ; panne après chaque DDL/marker ; ancien Board ouvre nouveau marqueur ; schema littéral altéré | Migration atomique/conservatrice ; vieux binaire refuse ; aucune réparation IFNOTEXISTS |
| UI Firefox synthétique | Desktop/mobile ; logout ownerchange ; timeoutreload ; CAS409 ; correction422 ; instance ready | Bodies/keys owner-scopés ; pas d’auto retry ; souhait≠connexion ; pas de login/prêt annoncé |
| Isolation globale | Ancien Runtime465 fake nonclean + famille verrouillée + listener désactivé | Rows/configs inchangés, compteurs provider/CLI/registration/feed/activation=0 |

Fixtures : vrais Board/SQLite temporaires et acteurs humains/devices synthétiques ; transport REST/MCP local, fournisseur/CLI/modèle fake compteurs0. Deux connexions et ordre de commit déterministe pour courses. Pannes avant/après chaque COMMIT/retour ; snapshots rows+marqueurs, aucun IFNOTEXISTS réparateur. Fichiers futurs A : nouveau `server_provisioning.py`, mixin/migration Board atomique, routes humaines app.py, guards server_instances.py, UI/tests dédiés. Pas de nouvel outil MCP/client manager A. La table est un plan : aucun de ces futurs tests produit n’est annoncé passé.
