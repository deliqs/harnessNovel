# Writing quality remediation plan

## Outcome

Raise generated-writing quality by preserving canon and planned beats throughout the pipeline, extracting usable craft guidance from references, validating generations before replacing artifacts, and making model/privacy/configuration behavior explicit. Preserve the existing CLI, Web, workspace, resume, and legacy-reading contracts.

## Guardrails

- Python 3.9+, standard-library offline tests, and no live model calls.
- Existing valid artifacts survive empty/invalid forced generations.
- English remains the emitted heading/filename format; supported Chinese and historical forms remain readable.
- `Stage` and `Phase` remain distinct concepts.
- Reference text is evidence and craft guidance, never prose to imitate or copy.
- Workers own disjoint files in the shared checkout and do not run git state-changing commands.
- The lead reviews and verifies but does not edit product code.

## Work units

### A. Generation pipeline and quality gates

Owner: Codex pipeline worker.

- Add a deterministic canonical story-context projection used by story arcs, chapter outlines, drafting, and revision.
- Carry previous generated arcs, stage/arc progress, remaining obligations, character/world constraints, and compact recent continuity without restoring the removed extra context-extraction LLM call.
- Plan stage arcs and per-arc chapter beats before independent generation; use reference lengths as priors/ranges, not exact structural mandates.
- Add deterministic quality diagnostics for heading/number/title, word-count range, POV/tense signals, forbidden style, outline/canon term retention, premature reveals, and reference similarity.
- Gate humanizer rewrites: keep the original draft when the rewrite is empty, invalid, fact-dropping, or materially more reference-similar; emit diagnostics.
- Make writes atomic and dependency-aware. Record sidecar provenance/dependency hashes, mark stale downstream artifacts, and never delete valid forced-generation output before a replacement validates.
- Fix English paragraph formatting using sentence/word-aware logic.
- Split cohesive helpers out of the large adaptive pipeline where this work naturally permits it.
- Add focused offline tests.

### B. Reference analysis, craft guidance, and compatibility cleanup

Owner: Codex reference worker.

- Extend chapter-card extraction to include entities, POV/tense, scene/rhythm/craft observations, evidence/confidence, and source locations.
- Build a compact reference craft/style bible that describes transferable techniques while explicitly prohibiting phrase/sentence reuse.
- Reject or clearly quarantine incomplete placeholder segments instead of promoting them as valid references.
- Make segmentation robust when a story unit exceeds the historical chapter cap.
- Treat reference phase/chapter counts as guidance rather than hard equality where this code owns the constraint.
- Fix front-biased prompt truncation so later obligations are retained.
- Retire unreachable private legacy outline code or restore its missing contract only if it remains reachable.
- Fix the English truncation-marker test contract.
- Package runtime prose assets (`system_prompt.md`, `agents.md`) and add packaging tests.

### C. Model roles, privacy/security, interface parity, and docs

Owner: Codex platform worker.

- Add optional draft/editor/critic model roles with backward-compatible fallback to the existing lite role; keep CLI and Web configuration synchronized.
- Add generation metadata needed for reproducibility/quality reporting without breaking callers.
- Make prompt tracing explicitly configurable, private by default, permission-safe, and bounded/redacted where feasible.
- Write credential/config files with user-only permissions.
- Delimit imported/user/reference content and document the instruction hierarchy at shared ingress points.
- Add an offline quality-report/evaluation entry point and representative fixtures so quality changes can be compared without paid calls.
- Reconcile README and example configuration with the actual current Web/CLI workflow, model roles, style extraction, audit behavior, and detector claims.
- Add lightweight dependency/CI hygiene only where it is compatible with the repository’s standard-library workflow.
- Add focused offline tests.

## Review and verification

1. Review each unit against its brief, scope boundary, compatibility contracts, and test quality.
2. Return concrete defects to the same worker thread; do not repair worker code in the lead session.
3. Run each affected test module, then:
   - `python3 -m unittest discover -s tests -v`
   - `python3 -m compileall -q core training webui novel_cli.py`
   - `python3 novel_cli.py --help`
4. Inspect the final combined diff for accidental overlap, generated files, secrets, and weakened checks.
5. Report any finding that cannot be closed without a product decision as an explicit residual, not a silent omission.
