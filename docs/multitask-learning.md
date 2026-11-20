# Persistent multi-task experience accumulation

Normal continuous learning uses one persistent Host state-root for the authorized
project/learning scope. Each new task still receives its own Run, workspace copy,
permissions, budgets and delivery. Do not create a new Host state-root per task
when the purpose is to measure cross-task learning. Separate A and B roots remain
appropriate for a control experiment; within B, successive tasks share its root.

```python
# Open the same state-root again after restarting the process.
app = RepoCode(state_root, provider, sandbox="docker", image=image)
sessions = app.session_experiences()
collection = sessions.create_collection("project:my-project", "continuous-learning")
for task in tasks:
    run = app.new_task(task.goal, task.workspace, task.contract, task.budget,
        authorize_write=True, authorize_process=True,
        session_id=collection, task_family=task.applicability_family)
    report = await app.execute(run["id"])
    index = sessions.accumulation(collection)
```

`create_collection` is deterministic by exact scope/name and survives process
restart. It allows different workspace roots in the explicitly bound collection.
It has no seven-day expiry. Closing it prevents reuse; calling create again does
not reopen it. Binding remains Host-only and must precede model/tool dispatch.
Distinct scopes/collection names do not implicitly attach to tasks.

Host-checked local stage reviews and registered verification results create
signed Episode candidate references. Each reference preserves the checked
snapshot and event boundary. Ordinary task endings and raw tool errors do not
create candidates.

At a later checkpoint, the embedding calls the collection's batch Curator.
It reads the original DEV evidence, expands Episodes and saves any grounded
Drafts. The same embedding can then submit those Drafts to the existing
qualification path. See the [integration example](L1-COLLECTION-LEARNING-LINK.md).

The accumulation index reports member runs, distinct task count, raw tool
errors, stage candidates, authorized Episodes and native patterns. Inspection
does not call a model. Patterns group observations and do not establish a
common cause.

CLI, using the same `--state-root` for every command:

```text
session create-collection --scope project:my-project --name continuous-learning
run --workspace WORKSPACE_1 --goal GOAL_1 --session-id COLLECTION --task-family FAMILY
run --workspace WORKSPACE_2 --goal GOAL_2 --session-id COLLECTION --task-family FAMILY
session accumulation COLLECTION
```

Collection creation/inspection makes no model calls. Curator execution, novelty
checks, real semantic validation, session admission and permanent G3/G4/G4b
Release checks still apply. Accumulation is not automatic capability activation
or evidence of improved performance. Only admitted session/project skills or
approved Release skills enter the existing CapabilityRouter. This change does
not turn the prior experimental planning-policy append into routed Skills.
Memory v1 remains the default and no datasets are shipped or downloaded.
