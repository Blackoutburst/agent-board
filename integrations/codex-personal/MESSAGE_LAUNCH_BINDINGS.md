# Bindings de lancement messages — fondation pure

`message_launch_bindings.py` vérifie uniquement la **cohérence structurelle** de données. Il ne lit ni n’écrit aucun fichier/journal, n’interroge aucun service et ne crée aucune permission, migration ou transition d’append. Aucun autre composant produit ne l’utilise dans ce lot. Les fonctions ne valident ni le propriétaire réel, ni la release installée, ni la fraîcheur du catalogue, ni l’appairage, ni un nettoyage.

## Entrées et résultats

Les entrées sont des dict/list et scalaires JSON de types exacts ; un texte JSON brut n’est pas une API acceptée. Le futur parser de journal/wire doit refuser les clés JSON dupliquées avant cet appel. Les sorties sont des copies détachées, sérialisables et sans autorité. Toute réutilisation par une fonction est intégralement revalidée ; un checksum recalculé n’adopte aucune donnée incohérente.

Le validateur structurel accepte la forme messages2 canonique : 1..16 cibles, types/champs fermés, bornes existantes des pages/budgets/instructions/modèles, ID/profil/session distincts et SHA corrects. Les chemins sont seulement des textes absolus, sans contrôles et <=4096octets ; aucune résolution, expansion, lecture, existence ou absence de symlink n’est attestée. Un ancien chemin ne satisfaisant pas ce profil structurel est refusé, jamais normalisé. Il n’appelle pas `validate(bind_connections=False)`, qui utilise encore Path.is_symlink.

Les hashes utilisent exactement les octets JSON UTF-8 v1 (clés triées, sans espaces, nombres finis). bool/int et 1/1.0 ne sont pas interchangeables. Configuration<=256Kio ; <=16origines, <=256Kio chacune et <=4Mio cumulées ; arbre borné en profondeur/nœuds/taille avant copie. Le catalogue attendu est comparé comme un ensemble trié par catalog_key ; l’ordre des targets, origines et bindings reste strict.

## Forme fermée

Le state `messages-launch-bindings-v1` contient : schema, configuration_sha256, source_release_sha256, origins, bindings, origins_sha256, bindings_sha256, state_sha256. Les manifests de digests référencent les snapshots entiers validés ; le hash global est toujours celui de la vraie configuration courante.

Chaque origine contient exactement `kind,operation_id,configuration_sha256,configuration` :

- La première a `kind=anchor` et porte C0, éventuellement **plusieurs anciennes cibles**. Toutes sont liées à l’opération d’ancrage actuelle. Aucun ancien birth_intent B n’est inventé.
- Chaque suivante a `kind=append`, ajoute exactement une cible en dernière position et conserve les globals et le préfixe par **octets canoniques**. Les longueurs sont k,k+1,…,n. Les ID d’opération sont des UUID canoniques distincts ; les snapshots sont uniques et aucune origine inutilisée n’est acceptée. La nouvelle cible exige start_at=now et workspace/connection_file distincts des précédentes.

Chaque binding contient exactement `kind,operation_id,target_id,target_sha256,origin_configuration_sha256,source_release_sha256,launchspec_sha256`. Les bindings suivent l’ordre de la configuration : les cibles C0 sont toutes anchor, puis un seul binding append pour la dernière cible de chaque origine suivante. Les références d’opération évitent un cycle de hash intent→bindings→intent ; ce ne sont pas des reçus d’exécution.

La formule reste L1(T,C,R)=digest({release:R,configuration:digest(C),target:T}). Les bindings hérités calculent L1 sur leur origine C0 ; la nouvelle cible utilise sa configuration de naissance. `launch_catalog` v1 n’est pas modifié. **R attendu est un argument obligatoire du caller et doit être identique partout**. Le module compare ce pin, mais ne l’authentifie pas : un caller qui fournit ensemble une release et des données forgées cohérentes n’obtient qu’une cohérence, pas une preuve de source. Aucun changement de release n’est une opération de ce module.

## API

```text
structural_configuration(configuration) -> copie structurelle
seed_legacy_anchor(C0, *, expected_release_sha256,
                   expected_predecessor_entries, anchor_operation_id) -> state
check_binding_set(state, *, current_configuration,
                  expected_release_sha256) -> copie de state
binding_catalog(state, *, current_configuration,
                expected_release_sha256) -> entrées catalogue
append_binding(state, C1, *, previous_configuration,
               expected_release_sha256, expected_predecessor_entries,
               append_operation_id) -> nouveau state
```

Les seed/append comparent aussi le catalogue du prédécesseur fourni par l’appelant. Les origines et les bindings ne sont jamais reconstruits à partir d’un hash manquant. L’échec est `BindingConsistencyError`, code fixe sans données d’entrée. Aucun `can_start`, ready, receipt, budget, horodatage ou handle de verrou n’est produit.

## Préconditions du futur raccord B

Le caller devra établir séparément les sources/outils protégés, le vrai owner/endpoint/host_instance, la configuration/fichiers vivants, les bindings Board, les Stop humains et l’absence d’enfants/deliveries en attente. Une mise à jour de source explicite devra précéder B si la release courante ne correspond pas au catalogue existant. Ancien lecteur, journal, coordinateur source-only et entrée protégée devront refuser le nouveau profil tant que leur raccord n’est pas revu.

**Les barrières A et B devront s’exclure symétriquement dans la même transaction Board** : create_A refuse une barrière B active et l’admission B refuse A. Ce contrôle n’existe pas dans ce module sans Board. Les demandes/grants A `metadata_request_only_v1` ne deviennent jamais des ordres/grants B ; un nouvel acte humain et une reprise durable explicite restent nécessaires.

Les tests synthétiques vérifient parité v1, ancre multi-cibles, préfixes, release attendue, bornes/refus de documents rehashés, copies détachées et absence d’appels I/O. Ils ne qualifient ni une transition Host/Board, ni des identités réelles, ni un fournisseur ou un backend Linux.
