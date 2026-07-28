# Providers and accounts

Providers describe how a model family reaches CLIProxyAPI. Named accounts bind
a friendly name, provider, credential reference, account pool, and priority.
Secrets remain in CLIProxyAPI's private authentication directory.

## Add an account

Use the interactive wizard:

```bash
orichum provider configure
```

The wizard lists configured providers, runs the selected CLIProxyAPI login,
detects the new credential, and asks only for the account's display name, pool,
and priority. Credential filenames and contents are not displayed.

Run the wizard again for each additional account.

## Low-level commands

The separate commands remain available for recovery and automation:

```bash
orichum provider login codex
orichum provider login claude
orichum provider login antigravity
orichum provider login kimi
orichum config paths
ls ~/.orichum/auth
```

Register the credential by filename, not by copying its contents:

```bash
orichum provider account add \
  "Personal GPT" openai CREDENTIAL_FILE shared --priority primary
```

`CREDENTIAL_FILE` means the filename created by CLIProxyAPI inside Orichum's
auth directory. `shared` is the account pool in which the account is available.
Normal interactive setup does not require this manual path.

## Manage accounts

```bash
orichum provider accounts
orichum provider account rename ACCOUNT_ID "Work Claude"
orichum provider account priority ACCOUNT_ID secondary
orichum provider account disable ACCOUNT_ID
orichum provider account enable ACCOUNT_ID
orichum provider account remove ACCOUNT_ID
orichum provider account sync
```

Priority aliases are `primary` (100), `secondary` (50), and `reserve` (10).
Integers from 0 through 1000 are also accepted.

Automatic stack candidates select the highest-priority eligible account in the
first matching pool. Equal-priority accounts are rotated deterministically for
new sessions. A candidate locked to a named account never rolls over.

Display names appear in explicit account and route inspection output.
Credential filenames, route prefixes, tokens, and secrets are not printed.

For two accounts of the same provider and mixed Anthropic/Antigravity examples,
see [Multi-account routing](multi-account-usage.md).
