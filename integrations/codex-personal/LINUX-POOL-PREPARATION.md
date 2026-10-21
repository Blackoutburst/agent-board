# Préparation explicite d’un travail Linux du Pool

`blackoutburst_agents.linux_pool_preparation.LinuxPoolJobPreparer` prépare un
travail déjà soumis. La construction et `status(operation_id)` ne lancent
aucune qualification. Aucun driver ne l’appelle automatiquement.

Le contrôleur opérateur construit les objets concrets `WorkerPool`,
`DevelopmentPoolDriver` et `LocalGitWorkspaceController`, puis :

```python
job = pool.submit(request_id=operator_request_id, driver_id='development',
    payload={'project_id': project_id, 'ticket_id': ticket_id})
preparer = LinuxPoolJobPreparer(preparation_directory,
    pool=pool, driver=driver, workspaces=workspaces)
result = preparer.prepare(job['id'],
    project_id=project_id, ticket_id=ticket_id, paths=approved_paths,
    resource=approved_resource, actions=approved_actions,
    verifier_path=public_python_script, verifier_sha256=approved_script_sha256,
    test_name=approved_test_name, test_timeout_seconds=2,
    test_max_output_bytes=4096, executable=public_codex_executable,
    cli_version=approved_cli_version, interpreter=current_public_python,
    unshare=public_unshare, instruction=operator_instruction,
    model=operator_model, planner_budgets=planner_budgets,
    broker_budgets=broker_budgets)
```

Tous les paramètres viennent de la configuration locale protégée de l’opérateur.
Le payload du Pool contient seulement les deux identifiants sélectionnés.
Le grant Git complet, son propriétaire, sa base et la correspondance exacte
avec `resource`, les UUID distincts du Pool, les outils, les sources et les budgets
sont épinglés dans l’intention privée. Le premier reçu de registration épingle
ensuite le device et la version du profil Board sélectionnés.

Le ticket doit être `todo`, sans claim, délégation, dépendance ou attente humaine,
avec projet actif et affectations absentes ou correspondant au propriétaire et
au profil. Trois nouveaux clients enregistrent la même session ; aucun argument
`registration=` réservé aux familles n’est utilisé. Les lectures fraîches
`get_work`, `get_ticket` et du contexte borné refusent les changements pertinents
avant chaque qualification et avant la liaison finale.

L’ordre est : intention durable ; copie Git isolée ; copie du seul vérificateur
approuvé et workspace planner vide ; manifeste/broker et plans existants ;
qualification `SupervisedLinuxNamedPythonBackend` puis `LinuxPlannerPolicy` ;
relecture finale ; sélection fermée ; `driver.bind_selection`. Il n’y a ni claim,
réservation Run/Runtime, lancement de modèle, dispatch, commit ni publication.
Seul le verrou privé du travail couvre ces étapes ; aucune RPC ni qualification
ne se déroule sous le mutex global du Pool.

Un résultat `bound` autorise le contrôleur à envisager son dispatch explicite.
Le vrai worker revalide ensuite toutes les admissions et preuves. Une répétition
identique, tant que le travail reste en attente, lit seulement les identités et
les fichiers épinglés ; elle renvoie `historical=True` sans appel réseau, copie
Git, qualification ou liaison supplémentaire. Toute interruption ou erreur reste
`needs_review`, conserve les artefacts et refuse toute adoption ou reprise
automatique, y compris une liaison réussie dont le retour local a été perdu.
Ne pas dispatcher une tentative dont `prepare` a échoué. `status` expose
uniquement l’historique local de la préparation.

Les qualifications natives doivent avoir lieu sous le même UID et dans le même
contexte système que le futur run, notamment cgroup, seccomp et environnement de
l’unité. Une qualification effectuée hors de cette unité ne s’y transfère pas.
Cette bibliothèque n’installe ni entrée de service ni unité.

Le vérificateur est un script Python fixe de confiance, identifié par son SHA,
sur les seules copies d’inputs admises. Ce composant ne généralise pas pytest,
les commandes du dépôt, les outils, les imports ou la publication GitHub. Les
tests locaux emploient une Board SQLite et de vrais dépôts Git synthétiques,
avec les frontières des qualifications natives isolées ; ils ne constituent
aucune qualification Linux native ou accès modèle.
