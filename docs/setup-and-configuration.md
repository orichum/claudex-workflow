# Orichum installation, setup, and configuration

This guide covers the complete user journey from a machine without Orichum to
an installed, project-aware, multi-account and mixed-model configuration.

The three lifecycle commands have separate responsibilities:

| Stage | Command | Purpose |
|---|---|---|
| Install | `./install.sh` | Install or reconcile Orichum's local runtime and services |
| First-run setup | `orichum setup` | Create the first provider account, project mapping, usable model stack, and verified route |
| Ongoing configuration | `orichum configure` | Change accounts, backups, models, roles, project settings, or local readiness for an existing project |

Normal users should start with these guided commands. Low-level provider,
stack, context, and configuration-file operations remain available for
recovery, custom placement, and automation.

## Install Orichum

### Supported hosts

Release-accepted hosts are:

- macOS on Apple Silicon;
- Linux on x86-64 with a systemd user manager; and
- WSL2 on x86-64 with systemd enabled.

The installer recognizes macOS x86-64 and Linux arm64, but those paths have
not completed native release acceptance.

Required host commands are `bash`, `curl`, `gh`, `git`, `jq`, `python3` 3.10
or newer, `rg`, `tar`, `uv`, and Claude Code. Linux and WSL also require `ss`,
normally supplied by `iproute2`.

The host Python only bootstraps installation. The installed CLI and services
use Orichum's private CPython runtime.

### Run the installer

```bash
git clone https://github.com/orichum/orichum.git
cd orichum
./install.sh
```

The installer prepares the complete local runtime. It installs the `orichum`
launcher, private Python, CLIProxyAPI, Claudex, LeanCTX, the Atlassian MCP
adapter, native shell completion, and the owned loopback services. It also
provisions LeanCTX's CPU ONNX Runtime and validates dense semantic search.

Managed runtime and mutable state live below `~/.orichum`. The Git checkout
remains source code and the installer source for later upgrades or uninstall.

If no provider has been authenticated, installation still succeeds. The route
proxy remains intentionally inactive in `pending-provider-login` state and the
installer prints one required next command:

```bash
orichum setup
```

Normal output shows progress, results, and required actions. Complete technical
output is retained privately under `~/.orichum/logs/`. Stream it during the
operation only when needed:

```bash
./install.sh --verbose
```

### Reconcile or upgrade

A later plain installer run is an idempotent reconciliation:

```bash
./install.sh
```

It reuses verified components and repairs missing or damaged owned state
without intentionally refreshing every upstream tool.

Use an explicit upgrade when you want Orichum to resolve permitted releases,
refresh managed components, run their complete probes, and perform the full
doctor check once a provider route is available:

```bash
./install.sh --upgrade
```

Orichum preserves user-managed accounts, projects, model stacks, sessions, and
LeanCTX knowledge during reconciliation and upgrade.

### Uninstall or purge

Remove installed runtimes and services while preserving accounts, sessions,
project configuration, and LeanCTX project knowledge:

```bash
./install.sh --uninstall
```

Permanently remove the preserved Orichum configuration and private data as
well:

```bash
./install.sh --uninstall --purge
```

Purge is destructive. Use it only when the saved accounts, sessions, project
mappings, model configuration, and LeanCTX data are no longer needed.

## First-run setup

Run setup after installation:

```bash
orichum setup
```

You can supply an existing project root or parent directory directly:

```bash
orichum setup ~/projects
```

When the positional path is omitted, setup asks for a projects folder and
defaults to `~/projects`. The prompted default is created when missing. An
explicit positional path must already exist and must be a directory.

The projects folder is a parent context, not a restriction to one repository.
Repositories below it inherit the configured account availability and model
stack unless a more specific project context overrides them.

### Setup phases

Setup is resumable and performs only missing phases:

| Phase | What setup does |
|---|---|
| Authentication | Uses an existing active account or asks for a provider and completes its supported login flow |
| Account | Asks for a friendly account name and registers the first account as Primary in Orichum's internal shared availability group |
| Runtime | Reconciles owned services when the installed runtime is not ready |
| Projects | Creates or reuses the projects-folder context and associates available account groups |
| Models | Reuses a usable project stack or creates a compatible recommended stack from live models |
| Services | Runs the full doctor check and verifies that the selected project has usable live routes |

Credential filenames, internal account IDs, account groups, route prefixes,
and numeric priorities are deliberately hidden during normal setup.

### Choose a provider

Setup lists the providers declared by the installed release. The standard
configuration includes:

| Provider choice | Model families |
|---|---|
| Anthropic | Claude |
| Antigravity | Claude and Google |
| Kimi | Kimi |
| OpenAI | GPT |

Choose the provider that will supply the first controller route. Additional
providers and accounts can be added later with `orichum configure`.

### Authenticate locally or over SSH

Provider authentication always prints the URL that must be opened. On a local
desktop, Orichum may also ask the operating system to open it automatically.

In an SSH session:

1. Copy the displayed URL.
2. Open it in a browser on your own machine.
3. Complete the provider sign-in.
4. Copy the final callback URL from the browser.
5. Paste that complete callback URL into the `Callback URL` prompt.

Press Enter instead when the callback reached the waiting Orichum process
automatically.

Authentication creates a private credential inside Orichum. Setup then asks
for a friendly account name, such as `openai-personal` or `work-claude`. Tokens
and credential contents are never copied into the project repository.

If a compatible private credential already exists but has not been registered
as a named account, the guided flow can reuse it instead of repeating login.

### Configure the projects folder

Setup asks:

```text
Projects folder [~/projects]:
```

Choose a stable parent directory that contains, or will contain, the projects
that should inherit this default Orichum configuration. This is commonly
`~/projects`, `~/work`, or another team-specific parent.

Setup maps the folder once. Re-running setup reports it as already configured
instead of creating duplicate contexts.

### Create the recommended stack

Setup checks the live model catalogue exposed by the owned local gateway. If
the project does not already resolve to a usable stack, Orichum creates a
compatible recommended stack and assigns it to the project context.

This avoids a second model wizard during onboarding. Use `orichum configure`
after setup when you want one model everywhere, models by work type, or custom
models for each specialist role.

### Verify readiness

The final phase runs Orichum's doctor and verifies the complete route for the
selected projects folder. Successful setup ends with:

```text
Orichum is ready.
```

Start a session from a repository below the configured parent:

```bash
cd ~/projects/my-app
orichum
```

### Resume interrupted setup

Setup records durable progress. If authentication, service startup, stack
creation, or verification is interrupted, run the same command again:

```bash
orichum setup
```

Completed phases are shown as already configured and are not repeated.

On failure, normal output states the failed action, a bounded reason, the
command to retry, and the private diagnostic-log path. Stream the technical
details on the next attempt when necessary:

```bash
orichum setup --verbose
```

If setup still cannot complete, run:

```bash
orichum doctor
```

Do not manually delete authentication or configuration merely because setup
stopped. The resumable flow is designed to reuse valid completed work.

## Ongoing guided configuration

Run configuration from the project you want to change:

```bash
cd ~/projects/my-app
orichum configure
```

Or target another configured project explicitly:

```bash
orichum configure --project ~/projects/another-app
```

The command requires an interactive terminal and a directory that resolves to
an existing Orichum project context. `--verbose` streams technical diagnostics
during reconciliation while retaining the private log:

```bash
orichum configure --verbose
```

Configuration begins by showing the resolved project and these exact areas:

```text
Accounts and providers
Models and agents
Project settings
Review and repair
Advanced
Back
```

Choices modify an in-memory draft. Nothing is saved until **Review and repair**
is used to apply the draft.

## Accounts and providers

The **Accounts and providers** menu contains:

```text
Add an account
Configure a backup account
Change account preference
Enable, disable, or remove an account
Back
```

### Add an account

Use **Add an account** for another independent account or provider.

1. Choose a provider from the installed provider configuration.
2. Complete or reuse its SSH-safe authentication.
3. Enter a friendly account name.
4. Choose where it is available:
   - **Current project** uses the project's first current availability group.
   - **All shared projects** uses Orichum's shared availability group.
   - **Advanced placement** stops the guided account draft and directs you to
     the low-level account commands for a custom group.
5. Choose how Orichum should use the account:
   - **Preferred** makes it the preferred account for that provider.
   - **Additional equal-choice account** allows deterministic rotation for new
     sessions when otherwise equally eligible.
   - **Backup** gives it lower preference than the current primary account.

The wizard derives the internal placement and priority from these plain-language
choices. It does not ask for an account group or numeric priority.

Authentication is saved securely as soon as login succeeds. If you go back or
cancel before applying the configuration, the unregistered credential remains
available for reuse during a later guided run.

### Configure a backup account

Use **Configure a backup account** when a known project account needs an
explicit compatible fallback.

1. Choose an active primary account that is already used by the project.
2. Orichum fixes the provider to that primary account; a backup cannot
   accidentally be created through another provider.
3. Authenticate or reuse another credential for that provider.
4. Enter a friendly name for the backup.
5. Orichum derives the same availability group and a lower internal preference.

If the current stack locks any model candidate to the primary account, the
wizard also asks whether to:

- **Allow PRIMARY with BACKUP [recommended]**, removing the lock so automatic
  backup can be used; or
- **Keep the account lock; do not enable automatic backup**.

Fallback is frozen when a new logical session is created. It remains within
the same logical model and model family. Existing sessions are not rebound.

### Account maintenance currently handled by Advanced

The guided menu displays **Change account preference** and **Enable, disable,
or remove an account**, but the current release directs those operations to
**Advanced**. Use:

```bash
orichum provider accounts
orichum provider account --help
```

The low-level account commands support rename, priority, enable, disable,
remove, and credential synchronization operations.

## Models and agents

The **Models and agents** menu contains:

```text
Use Orichum's recommendation
Use one model for everything
Choose models by work type
Customize every role
Back
```

Every model comes from the gateway's live, numbered list. Large lists are
searchable. The wizard does not require typed model IDs and marks the current
selection.

### Use Orichum's recommendation

Orichum chooses compatible live models for the controller and all specialist
roles using the shipped recommendation policy.

### Use one model for everything

Choose one live model once. It is assigned to the controller and every
specialist role.

### Choose models by work type

Choose one live model for each work category:

| Work type | Roles affected |
|---|---|
| Controller | Controller |
| Research | Repository explorer and repository verifier |
| Review | Correctness critic |
| Architecture | Architecture advisor |
| Implementation | Implementation worker |

### Customize every role

Select roles individually and assign a live model to each:

- Controller
- Repository explorer
- Repository verifier
- Correctness critic
- Architecture advisor
- Implementation worker

This path supports mixed-model stacks, such as a GPT controller, lower-cost
research workers, a Claude correctness critic, and a higher-capability
architecture advisor.

The model list shows each model's provider and eligible named accounts. Model
availability is checked again immediately before the draft is saved.

## Project settings

The **Project settings** menu contains:

```text
Model profile or stack
Account availability
GitHub identity
Jira configuration
Another configured project
Back
```

### Model profile or stack

This is the fully guided project-setting path in the current release. Orichum
lists only stacks that have compatible live routes for the selected project.
The current stack is marked. Select another stack to add the assignment to the
draft.

If the current stack is no longer live, the wizard explains that state and
offers the compatible alternatives. If none are usable, no project-stack
change is drafted.

### Project settings currently handled by Advanced

**Account availability**, **GitHub identity**, **Jira configuration**, and
**Another configured project** currently direct users to **Advanced** while
their guided flows are being completed.

Use the focused commands instead:

```bash
orichum context --help
orichum context list
orichum context update --help
orichum context jira --help
```

To configure a different project with the guided areas that are already
supported, leave the wizard and run:

```bash
orichum configure --project PROJECT
```

## Review and repair

**Review and repair** shows the complete effective draft before any write:

- the target project folder;
- new or selected primary and backup accounts;
- the concrete model assigned to every controller and specialist role; and
- the notice that changes apply to new sessions while existing sessions remain
  unchanged.

### Apply a changed draft

When changes are pending, the final choices are:

```text
Apply changes
Go back
Cancel
```

- **Apply changes** refreshes the live model catalogue, validates the draft,
  writes the configuration transactionally, reconciles the local runtime, and
  verifies the project.
- **Go back** preserves the in-memory draft and returns to the configuration
  menus.
- **Cancel** exits without applying the draft. Completed authentication remains
  private and reusable.

If model availability changed while the wizard was open, Orichum names only
the affected roles and asks you to choose a currently live model for each. It
does not silently substitute a different model.

When several new accounts are part of one confirmed draft, application is
compensating: if a later account or configuration write fails, Orichum removes
the accounts created earlier by that failed application attempt.

### Repair without configuration changes

When no changes are pending, **Review and repair** verifies the selected
project. A healthy project reports that no changes are pending and the project
is ready.

If the project is configured but its owned local runtime is not ready, the
wizard offers:

```text
Reconcile local services
Back
```

Reconciliation runs the normal idempotent installer path and verifies the
project again before reporting readiness.

## Advanced

The **Advanced** area does not open another large wizard. It shows the exact
low-level help entry points:

| Area | Command |
|---|---|
| Accounts | `orichum provider account --help` |
| Providers | `orichum provider --help` |
| Models | `orichum stack --help` |
| Projects | `orichum context --help` |

Use Advanced for automation, custom account-group placement, direct account
maintenance, ordered stack candidates, named-account locks, GitHub identity,
Jira configuration, and other focused project-context operations.

## What configuration changes affect

Guided configuration updates durable local control-plane state and then
reconciles the owned runtime. The final review always states:

```text
Changes apply to new sessions. Existing sessions are unchanged.
```

An existing logical session keeps its frozen controller route, named account,
and at most one compatible fallback. Resume it when you want the same binding.
Start a new session to use changed account selection or model assignments. Use
an explicit fork when moving work to another stack or model family with a
bounded handoff.

Configuration, accounts, authentication, sessions, and project data are
machine-local private state. They must not be committed or copied into a
repository. Inspect their locations with:

```bash
orichum config paths
```

## Common workflows

### First installation and setup

```bash
git clone https://github.com/orichum/orichum.git
cd orichum
./install.sh
orichum setup
cd ~/projects/my-app
orichum
```

During setup, choose the provider, authenticate, name the account, and accept
or change the projects folder. The account placement, recommended model stack,
service reconciliation, and readiness check are automatic.

### Same-provider backup

```bash
cd ~/projects/my-app
orichum configure
```

Choose:

1. **Accounts and providers**
2. **Configure a backup account**
3. The existing primary account
4. The recommended automatic-backup policy when an account lock is present
5. **Review and repair**
6. **Apply changes**

Start a new session to receive the new frozen primary and compatible fallback.

### Mixed controller and specialist models

```bash
cd ~/projects/my-app
orichum configure
```

Choose:

1. **Models and agents**
2. **Choose models by work type** for a compact configuration, or **Customize
   every role** for full control
3. Models from the numbered live lists
4. **Review and repair**
5. **Apply changes**

The reviewed role table is the exact configuration that new sessions will use.

## Inspect and validate

Use these commands after setup or configuration:

```bash
orichum --version
orichum doctor
orichum config paths
orichum config show
orichum config validate
orichum context list
orichum context validate
orichum provider accounts
orichum stack list
orichum stack show STACK
orichum models resolve
orichum models validate
```

Inspect the command surface and native completion at any level:

```bash
orichum --help
orichum setup --help
orichum configure --help
orichum provider --help
orichum provider account --help
orichum stack --help
orichum context --help
```

## Recovery and troubleshooting

### Setup stopped

Run `orichum setup` again. Setup skips completed phases and prints the private
diagnostic path when a phase fails. Add `--verbose` only when live technical
output is needed.

### Configuration was cancelled after authentication

Run `orichum configure` again. The private unregistered credential can be
reused; cancellation does not silently register an account or apply the draft.

### Project is configured but not ready

Open **Review and repair** and choose **Reconcile local services**, or run:

```bash
./install.sh
orichum doctor
```

### Account or model is missing from the wizard

The wizard shows only accounts and model routes that are active, visible to the
project, and currently advertised by the owned gateway. Inspect:

```bash
orichum provider accounts
orichum stack available
orichum stack show STACK
orichum models resolve STACK
orichum doctor
```

### A model changed while reviewing

Apply rechecks live availability. Choose a replacement only for the roles that
Orichum identifies as invalid, then review and apply again.

### A new session did not use the changed configuration

Confirm the launch directory resolves to the intended project context and that
you started a new logical session:

```bash
orichum context list
orichum stack show STACK
orichum sessions
```

Resumed sessions intentionally keep their original immutable route.

For deeper diagnostics, see [Troubleshooting](troubleshooting.md),
[Providers and accounts](providers-and-accounts.md),
[Multi-account routing](multi-account-usage.md), [Model stacks](model-stacks.md),
and [Project contexts](project-contexts.md).
