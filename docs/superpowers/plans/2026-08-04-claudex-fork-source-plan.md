# Claudex Fork Source Implementation Plan

> Historical implementation record. Completed through Claudex `v0.2.5` and
> Orichum `v0.1.0-rc.9` on 2026-08-04. Preserved at the user's request before a
> workstation reset.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Orichum install the latest verified `alupao/claudex` release and publish the resume and route-proxy fixes as the next Orichum release candidate.

**Architecture:** The Claudex fork remains synchronized with upstream and publishes standard Claudex release assets. Orichum changes only its trusted GitHub repository identity; the existing latest-release resolution, platform asset selection, checksum verification, probing, transactional activation, and rollback paths remain unchanged.

**Tech Stack:** Rust/Cargo, Bash, Python `unittest`, GitHub Actions and Releases, Orichum transactional installer.

## Global Constraints

- Resolve the latest GitHub release from `alupao/claudex`; do not pin a Claudex version.
- Preserve existing asset naming and SHA-256 verification.
- Do not add a user-facing source override or fallback to `StringKe/claudex`.
- Keep upstream copyright attribution unchanged.
- Do not commit `docs/superpowers/` planning artifacts.

---

### Task 1: Publish the Claudex fork release

**Files:**
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/claudex/Cargo.toml:3`
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/claudex/Cargo.lock:409-411`

**Interfaces:**
- Consumes: merged resume-hint implementation at `alupao/claudex` `main`.
- Produces: latest GitHub release `v0.2.5` with the repository’s standard platform archives.

- [ ] **Step 1: Bump only the fork package version**

```toml
[package]
name = "claudex"
version = "0.2.5"
```

Update only the `[[package]] name = "claudex"` entry in `Cargo.lock` to `0.2.5`.

- [ ] **Step 2: Run the contributor-required verification**

Run:

```bash
cargo fmt --check
cargo clippy -- -D warnings
cargo test
cargo check
```

Expected: zero failures and zero Clippy warnings.

- [ ] **Step 3: Publish the version bump through a focused PR**

```bash
git switch -c chore/release-0.2.5
git add Cargo.toml Cargo.lock
git commit -m 'chore(release): 发布 0.2.5'
git push -u origin chore/release-0.2.5
```

Create and merge a PR to `alupao/claudex:main` using the repository template.

- [ ] **Step 4: Tag and verify the release assets**

```bash
git switch main
git pull --ff-only origin main
git tag -a v0.2.5 -m 'v0.2.5'
git push origin v0.2.5
```

Wait for `.github/workflows/release.yml`, then verify the published release is non-draft and contains the macOS ARM64 and Linux AMD64 archives expected by Orichum.

---

### Task 2: Switch Orichum installer provenance

**Files:**
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/orichum/install.sh:895`
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/orichum/install.sh:1328-1333`
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/orichum/install.sh:2977-2982`
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/orichum/tests/test_install_state.py:24-29`
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/orichum/tests/test_installer.sh`

**Interfaces:**
- Consumes: latest `alupao/claudex` GitHub release using `claudex-v` tags and `-${arch}-${os}.tar.gz` assets.
- Produces: install-state identities beginning with `github:alupao/claudex@`.

- [ ] **Step 1: Add failing provenance assertions**

Add focused assertions to `tests/test_installer.sh`:

```bash
rg -Fq 'alupao/claudex' "$ROOT/install.sh"
if rg -Fq 'github:StringKe/claudex@' "$ROOT/install.sh"; then
  fail 'legacy Claudex provenance remains trusted'
fi
```

Change the default test manifest fixture to:

```python
source: str = "github:alupao/claudex@v0.2.5"
```

- [ ] **Step 2: Run the focused tests and observe failure**

Run:

```bash
bash tests/test_installer.sh
python3 -m unittest tests.test_install_state
```

Expected: installer provenance assertion fails while `install.sh` still trusts `StringKe/claudex`.

- [ ] **Step 3: Replace the three trusted source identities**

In `install.sh`, replace only:

```bash
github:StringKe/claudex@
StringKe/claudex
github:StringKe/claudex@
```

with:

```bash
github:alupao/claudex@
alupao/claudex
github:alupao/claudex@
```

Do not alter `stage_github_binary`, release selection, archive validation, or rollback logic.

- [ ] **Step 4: Run focused and contract verification**

Run:

```bash
bash tests/test_installer.sh
python3 -m unittest tests.test_install_state tests.test_route_proxy
python3 -m unittest discover -s tests -p 'test_*.py'
bash tests/test_smoke.sh
```

Expected: all commands exit zero.

- [ ] **Step 5: Perform a live explicit upgrade**

Run:

```bash
./install.sh --upgrade
orichum doctor
orichum --version
```

Verify `~/.orichum/state/install-state.json` records a source identity beginning with `github:alupao/claudex@`, the installed Claudex version is `0.2.5`, and a live `orichum resume` exit prints only the Orichum command.

---

### Task 3: Publish the Orichum change and release candidate

**Files:**
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/orichum/CHANGELOG.md`
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/orichum/VERSION`
- Modify: `/Users/arvind.thevar/Documents/claudex-workflow/orichum/docs/release-readiness.md`
- Modify: existing release-version assertions in `/Users/arvind.thevar/Documents/claudex-workflow/orichum/tests/`

**Interfaces:**
- Consumes: merged installer/resume/route-proxy implementation and published Claudex fork release.
- Produces: merged Orichum feature PR and published `v0.1.0-rc.9` prerelease.

- [ ] **Step 1: Publish and merge the feature PR**

Create `agent/fix-route-attestation-resume`, stage only the four implementation files plus the focused installer provenance changes, commit tersely, push, create a ready PR, wait for required checks, and merge without force-pushing.

- [ ] **Step 2: Prepare release metadata in a separate PR**

Set `VERSION` to `0.1.0-rc.9`. Move the route-attestation, Orichum-owned resume footer, and fork-provenance entries from `Unreleased` into `0.1.0-rc.9` dated `2026-08-04`. Update existing release-version tests without changing unrelated readiness claims.

- [ ] **Step 3: Verify, merge, and tag**

Run the standard contract and focused native install checks, merge the release PR, then create `v0.1.0-rc.9` as a GitHub prerelease targeting the merged `main` commit.

- [ ] **Step 4: Reinstall the published release locally**

Run the release installation path, confirm `orichum --version` reports the clean release, `orichum doctor` passes, concurrent route requests remain healthy, and `orichum resume` prints only the Orichum-owned command.
