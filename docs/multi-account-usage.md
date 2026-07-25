# Multi-account routing

Orichum can use multiple accounts from the same provider, or accounts from
different providers, without changing the machine-wide active login.

## Mental model

| Term | Meaning |
|---|---|
| Provider | The upstream service, such as `openai`, `anthropic`, or `antigravity` |
| Named account | One registered credential with a display name and priority |
| Pool | A project-visible group such as `shared` or `xebia` |
| Stack candidate | A model/provider/account policy available to a controller or agent role |
| Logical session | An immutable primary route plus at most one compatible fallback |

## Add multiple accounts for one provider

Log in once for each provider account. Each login creates a separate credential
file under the private auth directory shown by `orichum config paths`.

```bash
orichum provider login claude
orichum provider login claude
orichum config paths
ls ~/.local/share/orichum/auth
```

Register each credential by its filename, not its full path:

```bash
orichum provider account add \
  "Work Claude" anthropic FIRST_CREDENTIAL_FILE xebia --priority primary

orichum provider account add \
  "Backup Claude" anthropic SECOND_CREDENTIAL_FILE xebia --priority secondary
```

The same pattern works for two OpenAI accounts:

```bash
orichum provider login codex
orichum provider login codex

orichum provider account add \
  "Primary GPT" openai FIRST_CREDENTIAL_FILE shared --priority primary

orichum provider account add \
  "Backup GPT" openai SECOND_CREDENTIAL_FILE shared --priority secondary
```

Priority aliases are `primary` (100), `secondary` (50), and `reserve` (10).
Numeric priorities from 0 through 1000 are also accepted.

## How selection works

For an automatic candidate, Orichum:

1. Checks account pools in the order configured for the project.
2. Keeps active, healthy accounts that can serve the candidate's provider and
   model.
3. Selects the highest priority in the first eligible pool.
4. Rotates new sessions deterministically when multiple accounts share that
   priority.

A named-account lock always selects that account. It does not roll over to
another account.

## Configure a stack with the wizard

```bash
orichum stack available
orichum stack configure
```

The wizard lists models currently advertised by the local inference gateway.
For each controller or agent candidate, choose:

- **Automatic within provider** to let Orichum select an eligible account.
- A **named account** to lock the candidate to that account.

Review and save the stack, then optionally assign it to the current project.
The wizard rechecks live availability immediately before saving.

## Using accounts from different providers

Claude models can be available through both `anthropic` and `antigravity`.
Configure separate stack candidates and choose the provider explicitly in the
wizard. Normal wizard-created automatic candidates stay within their selected
provider; they do not silently switch from Anthropic to Antigravity.

```bash
orichum provider login claude
orichum provider login antigravity

orichum provider account add \
  "Direct Claude" anthropic CLAUDE_CREDENTIAL_FILE shared --priority primary

orichum provider account add \
  "Antigravity Claude" antigravity ANTIGRAVITY_CREDENTIAL_FILE shared \
  --priority primary
```

Use different providers for different roles in one stack, or create an explicit
new session/fork with another stack when you want to move the controller:

```bash
orichum models stacks
orichum fork SESSION_ID \
  --stack TARGET_STACK \
  --handoff-file ./bounded-handoff.md
```

## Immutable sessions and recovery

At session creation, Orichum freezes the selected primary route and at most one
fallback using the same logical model and family. Editing priorities, deleting
or reassigning a stack, or resuming later does not rewrite that binding.

Use a new session to apply updated account selection. Use an explicit fork when
changing stack or model family while preserving a bounded handoff.

## Inspect and troubleshoot

```bash
orichum provider accounts
orichum stack list
orichum stack show STACK
orichum sessions
orichum session routes SESSION_ID
orichum models stacks
orichum doctor
```

If an account does not appear in the wizard, verify that it is active, belongs
to a pool visible to the project, and advertises the selected provider/model
route. Configuration file responsibilities are listed in the
[README control-plane table](../README.md#control-plane).
