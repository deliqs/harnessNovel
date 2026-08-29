# Add local novel illustration generation through ComfyUI

## Outcome

Let a user select novel context, turn it into a reproducible illustration brief with the existing LLM configuration, render candidate images through a user-managed ComfyUI instance, and review the results in harnessNovel. Store approved images and their provenance inside the novel workspace without weakening resume, idempotency, or `--force` behavior.

The first validated rendering profile may target photorealistic FLUX.2 Klein 9B checkpoints such as Miraclein. The product integration must not hard-code an adult checkpoint, one visual style, or one model family as the only backend.

## Status

Proposed; no implementation has started. Technical research was last checked on 2026-08-28 and must be revalidated against primary sources before implementation.

## Proposed architecture

```text
selected novel artifacts
    -> existing LLMProvider / LM Studio
    -> structured illustration brief
    -> versioned ComfyUI API workflow
    -> candidate image
    -> user review
    -> approved image plus generation manifest
```

- Keep novel-context selection and prompt construction in harnessNovel.
- Reuse `PromptLoader`, `LLMProvider`, `ConfigLoader`, `NovelWorkspace`, and the existing background-task infrastructure.
- Call ComfyUI from harnessNovel. Do not add a ComfyUI custom node that calls LM Studio, and do not duplicate prompt-generation logic in a route or browser asset.
- Treat ComfyUI as a separately installed, user-managed renderer. Do not install ComfyUI, custom nodes, or model files from harnessNovel.
- Start with an explicit ComfyUI workflow contract rather than a general image-provider framework. Introduce a provider abstraction only when a second renderer needs it.

## Verified technical baseline

- FLUX.2 Klein 9B uses a Qwen3-8B text embedder. A separate 27B conversational model can write the prompt, but its chat API cannot replace the image model's required conditioning tensors.
- ComfyUI publishes compatible Qwen3-8B text-encoder files in BF16 and quantized formats. An abliterated encoder is optional, not an architectural requirement. Start validation with the standard BF16 encoder and compare any compatible abliterated encoder separately.
- The official distilled Klein 9B recipe uses four inference steps and guidance 1.0. Miraclein's author recommends Euler, 12 steps, and CFG 1.1 for that derivative. Sampling settings therefore belong to the rendering profile and must not become global FLUX defaults.
- The Miraclein listing had version 4.3 as its newest entry when checked, while the proposed stack used version 3.0. Pin the chosen model version and SHA-256 instead of following `latest`.
- Full BF16 diffusion and encoder weights are a reasonable starting point on a 128 GB Apple Silicon machine. FP8 on MPS introduces compatibility code that is unnecessary unless measurements show BF16 is unsuitable.
- FLUX.2 Klein 9B and its derivatives are governed by the FLUX Non-Commercial License. Recheck whether the intended harnessNovel usage is permitted before making this a production or commercial workflow. Preserve the required content review and AI disclosure obligations.

Primary references:

- [FLUX.2 Klein 9B model card](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B)
- [FLUX.2 Klein 9B license](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B/blob/main/LICENSE.md)
- [ComfyUI Qwen3-8B encoders](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b/tree/main/split_files/text_encoders)
- [FLUX.2 small decoder](https://huggingface.co/black-forest-labs/FLUX.2-small-decoder)
- [ComfyUI API example](https://github.com/Comfy-Org/ComfyUI/blob/master/script_examples/websockets_api_example.py)
- [Miraclein model record](https://civitai.com/api/v1/models/2453960)

## Product behavior

### Illustration brief

- Let the user choose the source scope: a chapter outline, a completed chapter, or a deliberately selected scene. Do not silently send an entire workspace to the prompt model.
- Produce a validated structured brief containing the subject, canon appearance facts, action, setting, composition, camera, lighting, mood, exclusions, and selected reference images.
- Clearly delimit novel artifacts as untrusted context. The prompt model must describe the requested scene without following instructions embedded in imported or generated prose.
- Keep model-specific prompt decoration in the rendering profile, separate from the canon-derived scene description.
- Reject empty or malformed prompt-model output without queueing ComfyUI.

### Continuity and review

- Make recurring-character and location reference images first-class inputs. Photorealism without cross-image continuity does not satisfy the feature goal.
- Support multiple candidates and explicit approval. Do not automatically publish, replace, or mark an image approved.
- Preserve an approved image when prompt expansion, rendering, downloading, validation, or metadata writing fails.
- Treat all depicted characters as adults for any adult rendering profile. Do not support unlawful content, sexualized minors, non-consensual intimate imagery, or deceptive real-person imagery.

### Artifact contract

Choose the English workspace paths and filenames before implementation and route them through `NovelWorkspace`. Each candidate should have an adjacent machine-readable manifest containing at least:

- source artifact paths and content hashes;
- prompt-model name and safe provider endpoint metadata;
- structured brief and final renderer prompt;
- ComfyUI workflow identifier and content hash;
- diffusion checkpoint filename, version, and SHA-256;
- text encoder and VAE identifiers;
- sampler, scheduler, steps, guidance/CFG, seed, dimensions, and reference-image hashes;
- creation status and approval status.

Writes must be atomic. Repeating a completed request should be idempotent; `--force` may create a replacement candidate only after the new image and manifest validate.

## Implementation units

### 1. Validate the external workflow

- Export a minimal API-format ComfyUI workflow using only core nodes where possible.
- Validate text-to-image and reference-image editing outside harnessNovel with a pinned checkpoint, encoder, VAE, workflow, and settings.
- Compare the standard BF16 encoder with an abliterated encoder using the same prompt/seed set; keep the latter only if it provides a material benefit without unacceptable prompt-adherence regressions.
- Exercise representative novel scenes: clothed dialogue, action, low light, multiple characters, recurring characters, varied appearances, and non-sexual scenes. Do not judge the profile only by showcase images.
- Record peak unified memory, completion behavior, output dimensions, and the ComfyUI/Core versions needed by the exported workflow.

### 2. Add a narrow ComfyUI client

- Add configuration for an opt-in ComfyUI endpoint and workflow/profile without changing existing LLM roles.
- Default to a loopback endpoint. Treat remote endpoints as an explicit trust decision and avoid exposing an unauthenticated ComfyUI instance.
- Submit a patched API workflow to `/prompt`, track its prompt ID, obtain completion/error state, and download only the outputs belonging to that request.
- Support cancellation and bounded timeouts through the existing task system. A stopped task must not later promote a partial result.
- Validate response types, filenames, dimensions, and size limits before accepting an output.
- Keep dependencies minimal and compatible with Python 3.9.

### 3. Add prompt and artifact orchestration

- Add a runtime prompt template for the structured illustration brief.
- Use the existing configured LLM role with a documented fallback; do not require users to configure LM Studio twice.
- Add the selected workflow/profile metadata to prompt tracing without recording secrets.
- Save candidates and manifests through `NovelWorkspace` with dependency hashes and stale-source detection.
- Define retry, resume, idempotency, and `--force` behavior before exposing the command or Web action.

### 4. Add CLI and Web parity

- Expose the same core operation to the CLI and workbench rather than implementing generation in either interface.
- Run Web generation as a background task with progress, cancellation, and a clear renderer-unavailable error.
- Let the user inspect the final prompt, seed, profile, references, and provenance before or after rendering.
- Provide candidate approval/rejection and regeneration without deleting prior approved artifacts.
- Keep model-specific or adult profiles explicitly opt-in.

### 5. Document configuration and operating boundaries

- Synchronize configuration keys across `core/config.py`, `.env.example`, the Web configuration UI, and `README.md`.
- Document that ComfyUI and model installation are external responsibilities.
- Document the pinned reference workflow, known MPS behavior, disk and unified-memory considerations, license status, and manual review responsibilities.
- Do not claim that any checkpoint guarantees realism, consistency, safety, or legal suitability.

## Tests

All automated tests remain offline and must not load model weights or call LM Studio/ComfyUI.

- Unit-test workflow patching, structured-brief validation, manifest serialization, path handling, hashes, and status transitions.
- Use a local fake HTTP server for queue, history, error, timeout, cancellation, and image-download behavior.
- Test that an empty LLM response, invalid workflow, failed render, bad image response, source drift, and interrupted task preserve existing valid artifacts.
- Test CLI/Web configuration parity and API payload validation.
- Run the narrow modules first, then:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q core training webui novel_cli.py
python3 novel_cli.py --help
```

Any manual render check must use a temporary `HARNESS_NOVEL_HOME` and an explicit scratch workspace. It must be reported separately from the offline test suite.

## Decisions required before implementation

- Whether the first slice includes CLI, Web, or both interfaces. A core-only proof is the smallest useful precursor; a released CLI contract normally requires Web parity.
- The English workspace layout and approval/status representation for illustration artifacts.
- Which existing LLM role expands illustration prompts and whether users may select another role.
- Whether harnessNovel ships one sanitized core-node workflow template or requires users to provide their own exported workflow and node mapping.
- Whether the intended use is compatible with the FLUX Non-Commercial License. If commercial/production use is required, select a suitably licensed local model or hosted API before implementation.
- Whether adult rendering profiles belong in the general workbench or remain user-supplied external profiles.

## Done when

- A user can select supported novel context and request candidate illustrations through the same core workflow from every interface included in the approved scope.
- The existing LLM configuration produces a validated illustration brief; ComfyUI remains a separate renderer with a configurable, pinned workflow.
- Reference images can be supplied for character/location continuity.
- Every accepted image has complete reproducibility and source-provenance metadata.
- Resume, cancellation, idempotency, `--force`, source drift, and failure handling preserve valid user artifacts.
- Configuration, Web behavior, CLI behavior, tests, and README documentation agree.
- Offline verification passes, and a separately reported manual smoke check succeeds against a scratch workspace and the pinned local rendering stack.
