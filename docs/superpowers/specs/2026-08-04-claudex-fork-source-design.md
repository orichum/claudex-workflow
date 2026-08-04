# Claudex Fork Source Design

> Historical design record. Implemented through Claudex `v0.2.5` and Orichum
> `v0.1.0-rc.9` on 2026-08-04. Preserved at the user's request before a
> workstation reset.

## Goal

Make Orichum install the latest published Claudex release from
`alupao/claudex` while preserving the installer’s existing provenance,
artifact-integrity, and fail-closed behaviour.

## Design

- Replace `StringKe/claudex` with `alupao/claudex` in the installer’s release
  resolver, trusted source-identity checks, and persisted install state.
- Continue resolving the repository’s latest GitHub release. Do not pin a
  Claudex version or add a user-facing source override.
- Preserve the existing release asset naming contract and SHA-256 verification.
- Treat an existing `github:StringKe/claudex@...` installation as prior state,
  not as trusted reusable provenance for the new source. The next explicit
  upgrade resolves and installs the latest `alupao/claudex` release.
- Keep upstream attribution in third-party notices; no unrelated README or
  documentation changes are required.

## Release Flow

1. Publish the tested resume-hint implementation as the latest release in
   `alupao/claudex`.
2. Merge the Orichum installer change.
3. Run installer and native acceptance against the fork release.
4. Publish the next Orichum release candidate.

## Failure Behaviour

- Missing fork release, missing expected platform asset, unsafe archive,
  checksum mismatch, or failed binary probe stops installation without
  replacing the active Claudex binary.
- A failed upgrade retains the previously installed Orichum runtime and
  credentials through the existing transaction rollback path.

## Verification

- Focused tests assert all trusted Claudex source identities use
  `alupao/claudex`.
- Installer tests cover migration from recorded `StringKe/claudex` provenance.
- The standard contract suite and native local install must pass.
- A live `orichum resume` exit must print only the Orichum-owned resume command.

## Scope

Only Claudex release provenance, its focused tests, fork release publication,
and the required Orichum release metadata are in scope.
