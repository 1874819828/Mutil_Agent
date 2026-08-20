# MVD Evaluation Rubric

Evaluation mode: `code-and-live-docker`  
Pass threshold: 8.0 / 10  
Maximum generator/evaluator iterations: 5

## 1. Functional vertical slice - weight 35%

- 10: create -> Manager plan -> approval -> workspace -> change -> tests -> review -> terminal report is exercised end to end.
- 8: Slice 1 and most downstream contracts work; one non-core path is deferred explicitly.
- 5: scaffolding exists but no persistent state transition is demonstrated.
- 0: application does not start or tests fail.

## 2. Correctness and independent oracle - weight 25%

- 10: baseline and external acceptance tests are immutable and independently gate completion.
- 8: oracle boundaries are implemented and unit-tested, with Docker integration environment-gated.
- 5: Developer-authored tests can determine success.
- 0: no executable correctness check.

## 3. Security and isolation - weight 25%

- 10: allowlist copy, canonical paths, fencing token, generation isolation, immutable artifacts, Docker hardening, authenticated local API, and no host fallback are implemented and tested.
- 8: all code-level controls exist; live Docker is skipped only when the daemon is unavailable.
- 5: security relies materially on prompts or denylists.
- 0: generated commands execute directly on the host or the original project is mutated.

## 4. Reproducibility and maintainability - weight 15%

- 10: pinned dependencies, pinned Docker base image, clear README, deterministic mock provider, typed contracts, focused modules, tests, and clean startup commands.
- 8: reproducible locally with minor manual setup.
- 5: undocumented environment assumptions.
- 0: cannot reproduce from a fresh terminal.

## Automatic failure conditions

- Original target project is changed.
- A stale worker generation can affect active state or files.
- Manager can expand allowed globs.
- A run reaches `completed` without immutable acceptance tests passing.
- Raw artifact paths are accepted from the API.
- Docker failure silently triggers host execution.
