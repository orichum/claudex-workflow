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

Run the provider wizard once for each account:

```bash
orichum provider configure
orichum provider configure
```

Choose the same provider both times. Name the first account, select its pool,
and give it primary priority. Give the second account secondary or reserve
priority. The same flow works for Claude, OpenAI, Antigravity, and Kimi
accounts without handling credential filenames manually.

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
Configure separate ordered stack candidates and choose each provider
explicitly in the wizard. The shipped architecture-advisor role demonstrates
this: Anthropic Opus 5 is the first startup candidate and Antigravity Opus 4.6
Thinking is the second.

Candidate fallback happens only while binding a new session. Normal
wizard-created automatic candidates stay within their selected provider, and
an existing session never silently changes model or provider.

```bash
orichum provider configure  # choose anthropic
orichum provider configure  # choose antigravity
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
route. Configuration file responsibilities are listed in
[Configuration](configuration.md).
