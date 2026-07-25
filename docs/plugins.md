# Plugins

Optional Claude Code plugins are declared in `plugins.json` and synchronized
into Orichum's private Claude configuration. They do not modify the user's
normal Claude Code plugin directory.

```bash
orichum plugin list
orichum plugin add PLUGIN@MARKETPLACE --source OWNER/REPOSITORY
orichum plugin sync
orichum plugin update
orichum plugin remove PLUGIN@MARKETPLACE
```

`add` records the marketplace and plugin declaration, then synchronization
makes it available to new Orichum sessions. `update` refreshes declared plugin
sources. `remove` removes the optional declaration.

The bundled `orichum-controller` plugin is not optional and does not appear in
`plugins.json`. Orichum materializes a private copy for every physical session
because it supplies the controller policy, audited agents, workflows, and
security hooks.

After changing plugins:

```bash
orichum plugin sync
orichum plugin list
orichum doctor
```

Existing physical sessions keep their immutable plugin copy. Start or resume a
new physical run to load updated plugin declarations.
