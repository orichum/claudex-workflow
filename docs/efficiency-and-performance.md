# Efficiency and performance

This report records controlled measurements from the 2026-07-27
release-candidate pass. Measurements are local observations, not marketing
estimates.

## Source-context savings

Token counts use the `o200k_base` tokenizer for a consistent comparison. The
same source bytes were measured before and after LeanCTX processing.

### Large Orichum module

Fixture: `integrations/common/orichum_cli.py`, 2,010 lines.

| Read mode | Tokens | Reduction from raw |
|---|---:|---:|
| Native/raw | 14,177 | — |
| LeanCTX full | 14,177 | 0% |
| LeanCTX signatures | 1,437 | 89.9% |
| LeanCTX map | 1,558 | 89.0% |

### Small module

Fixture: 106 lines, 3,375 bytes.

| Read mode | Tokens | Reduction from raw |
|---|---:|---:|
| Native/raw | 727 | — |
| LeanCTX full | 727 | 0% |
| LeanCTX signatures | 295 | 59.4% |
| LeanCTX map | 231 | 68.2% |
| LeanCTX anchored | 1,315 | -80.9% |

Anchored mode is an edit-safety mechanism, not a compression mode. Its hashes
cost tokens but allow `ctx_patch` to reject stale edits. Orichum therefore uses
map/signature/search modes for understanding and anchored mode only before a
patch.

## Tool-schema efficiency

Orichum keeps six tools resident in eligible model requests:

- native `Bash`;
- LeanCTX `ctx_read`, `ctx_search`, `ctx_expand`, `ctx_patch`, and `ctx_shell`.

Other native and project-specific tools are marked for deferred loading, and
Claude receives the tool-search primitive. Live acceptance showed the expected
sequence: `ToolSearch` loaded Graphify and Mempalace only when the prompt needed
them. This avoids injecting optional Graphify, Mempalace, and MCP_DOCKER schemas
into every model turn.

The exact LeanCTX MCP is intentionally limited to six tools. Four read-only
tools are preapproved; patch and shell keep normal approval. The universal
gateway, duplicate shell path, session memory, composition, and LeanCTX graph
paths are disabled because Orichum already owns those responsibilities.

## Prompt-cache evidence

A live logical-session resume consumed 807 new input tokens and reused 11,776
cached input tokens. The original session had consumed 24,658 new input tokens.
These prompts were not identical, so this is evidence that resume preserves
provider cache continuity—not a claim of a universal 96.7% saving.

## Live latency and cost samples

| Flow | API duration | Cost | New input | Cached input | Output |
|---|---:|---:|---:|---:|---:|
| GPT controller + LeanCTX read | 12.52 s | $0.134653 | 24,658 | 11,776 | 219 |
| Resume same logical session | 4.40 s | $0.011648 | 807 | 11,776 | 69 |
| Graphify + Mempalace live context | 9.45 s | $0.102776 | 17,947 | 18,432 | 153 |
| GPT/Terra explorer | 12.15 s | $0.055014 | 3,509 | 20,480 | 124 |
| Sonnet critic | 88.28 s | $0.214390 | 13,293 | 10,752 | 187 |
| Opus architect | 34.54 s | $0.207710 | 24,172 | 0 | 170 |
| Verifier + implementation worker | 46.37 s | $0.154055 | 6,187 | 43,008 | 484 |

These are single bounded samples on one network and account state. They show
relative workflow behavior, not guaranteed provider latency or price.

## Local resource use

At idle after all sessions exited:

| Shared service | CPU | Resident memory |
|---|---:|---:|
| CLIProxyAPI | 0.0% | 68,144 KiB |
| Orichum route proxy | 0.0% | 23,968 KiB |
| Total shared resident footprint | 0.0% | 92,112 KiB (about 90 MiB) |

No per-session Claudex translator, LeanCTX, Graphify MCP, or Mempalace MCP
process remained after the corresponding session ended. One-shot live session
process trees peaked at roughly 355–380 MiB RSS in the sampled runs.

The final in-place install/upgrade took 65.03 seconds on an Apple Silicon
Mac with warm package caches.

## Conclusion

The efficient daily-driver path is:

1. defer optional schemas;
2. use LeanCTX map/signature/search for understanding;
3. use anchored reads only for edits;
4. query Graphify for relationships;
5. query Mempalace only for durable history;
6. delegate only when independent specialist work justifies its latency and
   token cost.

This preserves the strong controller and full worker output while avoiding
always-on memory, graph, proxy, and optimizer duplication.
