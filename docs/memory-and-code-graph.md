# Memory and code graph

Mempalace and Graphify solve different problems:

- Mempalace recalls durable project decisions and conventions.
- Graphify describes current repository structure and relationships.

## Initial population

`orichum context add` or an explicit `orichum context populate ROOT` discovers
independent repositories, follows declared submodules, skips duplicate linked
worktrees, mines repository content into Mempalace, synchronizes central
Graphify graphs, and installs Orichum's Graphify refresh hooks.

Population is a foreground, explicit operation. Progress and elapsed time are
visible; it is not a resident indexing service.

Generated Graphify data and legacy repository-local `graphify-out` directories
are excluded from Mempalace mining. This avoids embedding a large generated
graph back into memory. It does not remove code structure from the workflow:
Graphify remains available through its own MCP.

Graph-only synchronization is also available independently of context
population:

```bash
orichum graph .
orichum graph ~/xebia
orichum graph status .
```

These commands do not invoke Mempalace.

## Identity and central storage

Orichum derives a repository identity from its unambiguous fetch remote,
preferring `origin`. Credentials are removed and URL forms are normalized, so
equivalent HTTPS and SSH clone URLs select the same identity. Repositories
without a remote receive a persistent local identity on the first graph sync.
Set or clear an override when automatic identity is unavailable or unsuitable:

```bash
orichum graph identity . --set github.com/xebia/X-ACE-UI
orichum graph identity . --clear
```

Graphify runs against the repository but writes only below Orichum's private
data directory. Use `orichum config paths` to locate that data directory and
`orichum graph status .` to see the exact selected output. Source paths stored
in the graph are repository-relative, so a clean graph can be reused after a
clone is moved.

Clean clones with the same repository identity and commit share one revision
graph. Dirty checkouts use separate working graphs keyed by a
persistent checkout identity and a fingerprint of their changes. This keeps
uncommitted states isolated between clones and linked worktrees, even when they
currently point to the same commit.

## Lifecycle and hooks

`orichum graph PATH` discovers repositories below `PATH` and creates or updates
the graph for each repository's exact current state. It leaves no active
Graphify output in the repository. Graphify runs with the repository as its
working directory, while `GRAPHIFY_OUT` points at private staging. Before
activation, Orichum validates repository-relative source paths and requires the
graph's `built_at_commit` provenance to equal the repository revision selected
for that graph state. Marked `post-commit` and `post-checkout` hook sections are
installed only after a graph is successfully activated or migrated, while
preserving unrelated user hook content. A not-applicable sync for a repository
with no supported code installs no hooks; run an explicit sync after supported
code is added. The hook launches a detached, serialized refresh, so Git does
not wait for Graphify extraction. Hook output is kept in a bounded private log.

Each successful sync also prunes only working graphs whose recorded checkout
path no longer exists. Revision graphs remain reusable, and working graphs for
existing checkouts are retained. Removing a temporary linked worktree therefore
makes its working graph eligible for pruning on a later sync for that repository
identity.

If a recognized repository-local `graphify-out` exists and no central graph is
active for that state, the next sync validates and migrates it transactionally.
Unknown or unsafe entries stop migration instead of deleting data. Central
storage is authoritative after migration; do not create or point tools at
repository-local Graphify output.

## During a session

- The controller recalls Mempalace only when prior decisions or durable
  conventions matter.
- A hook binds every Mempalace call to the verified project wing.
- Graphify queries are made on demand when a matching central graph was bound
  at session startup.
- Broad graph or memory payloads are not injected automatically into every
  prompt.

This on-demand design keeps the tools useful without paying their schema and
retrieval cost on unrelated tasks.

Session startup never builds, updates, migrates, or prunes graphs. It accepts a
central graph only when that graph is current and stable for the exact
repository state. Each physical session then copies the validated bytes to
private `run_dir/graph.json` with mode `0600`, records their digest in immutable
context, and points the Graphify MCP at that snapshot rather than central
storage.

An existing physical session remains on its snapshot generation when the
central graph changes. Materialization of a resume or other new physical run
retries once against the latest stable validated binding. A stable match is
snapshotted and includes Graphify; if no valid binding exists or instability
persists, the physical session is created without Graphify. Build the graph
first, then start a new session:

```bash
orichum graph .
orichum
```

## Maintenance

Graphify's installed Git hooks refresh normal repository changes. Run
`orichum graph .` for an explicit graph refresh. Run full population only after
adding repositories or when intentionally refreshing both project memory and
graphs:

```bash
orichum graph .
orichum context populate ~/xebia
orichum context validate
orichum doctor
```
