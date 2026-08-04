# LeanCtx Integration Efficiency Design

> Historical design record. Implemented and merged in Orichum PR #92 on
> 2026-08-04. Preserved at the user's request before a workstation reset.

## Goal

Reduce Orichum-managed LeanCtx prompt overhead without weakening its tool,
security, project-jail, cache, or session-resume contracts, and restore accurate
rolling economics for the official LeanCtx 3.9.12 ledger format.

## Observed state

- The active Orichum run used the `lean` profile and compressed tool results by
  95.6%.
- Orichum already deferred seven of eleven LeanCtx tools through provider-native
  tool search. The four resident schemas totalled 944 tokens.
- LeanCtx tool health reported client rule-file inventory even though Orichum
  already supplied controller policy and user/project rules.
- LeanCtx 3.9.12's local configuration schema documents
  `LEAN_CTX_RULES_INJECTION=off` for hosts that provide their own steering.
- Rolling economics rejected valid `bounce` correction records because those
  records intentionally carry negative `saved_usd` values.

## Selected design

Configure every Orichum-owned LeanCtx environment with
`LEAN_CTX_RULES_INJECTION=off`, retain the current tool profiles, and validate
official bounce corrections explicitly. Do not dynamically attach LeanCtx and
do not change the meaning of the persisted `lean` profile.

## Managed environments

The override applies to the private session MCP environment, the shared proxy,
installer and embedding probes, and generated launchd/systemd definitions.
Orichum remains the source of controller steering.

The service ownership contract is strict after activation. Installer preflight
also recognizes the exact immediately previous Orichum-owned definition so an
upgrade can replace it; any other environment drift is rejected.

## Bounce-aware economics

Ordinary compression and caching records retain nonnegative `saved_usd`.
Negative values are accepted only for exact official bounce records:

- mechanism is `compression`;
- tool is `bounce`;
- `actual_tokens == baseline_tokens`;
- `saved_tokens == 0`;
- `0 < bounce_adjustment <= baseline_tokens`;
- `saved_usd < 0` and finite.

Valid corrections are included in rolling compression USD. Orichum never
rewrites, truncates, or quarantines the upstream ledger.

## Compatibility

- Existing logical and physical sessions keep their profile semantics.
- All eleven LeanCtx tools remain advertised.
- Provider-native deferral and cache-control movement remain unchanged.
- Malformed records and unsafe or changing ledger files continue to fail
  closed.
