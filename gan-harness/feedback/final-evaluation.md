# Final MVD Evaluation

Date: 2026-08-12  
Result: PASS  
Score: 9.4 / 10

## Weighted score

| Area | Score | Weight | Contribution |
| --- | ---: | ---: | ---: |
| Functional vertical slice | 9.5 | 35% | 3.33 |
| Correctness and independent oracle | 10.0 | 25% | 2.50 |
| Security and isolation | 9.5 | 25% | 2.38 |
| Reproducibility and maintainability | 8.0 | 15% | 1.20 |
| Total |  |  | 9.40 |

## Evidence

- 97 tests collected; full suite passes. Two golden modules are intentionally executed in Docker, and two platform-specific filesystem cases are skipped on this Windows host.
- Application coverage is 86%, above the 80% gate.
- Final live run `aa2ca23c-9c6b-452c-ab78-d3772f248461` reached `completed` through the HTTP API, approval, generation-isolated workspace, Docker baseline tests, Docker acceptance tests, deterministic policy gate, and Reviewer approval.
- Baseline result: 2 passed, 0 failed.
- Acceptance result: 2 passed, 0 failed.
- Original source manifest before and after the run is identical: `96a6a6330af07de849f493da44d609e562eb967077ee7c20e26500fae33dcf4d`.
- Final runner digest: `sha256:c68abd59632adf81b95ccc7430c6d1e74513fd427bd17d9a10e7626db2277001`.
- Rebuilding the pinned Dockerfile twice with `--provenance=false` produced the same digest.
- Fixed launcher binds `127.0.0.1:8080`; HTTP smoke checks return 200 for `/health`, 401 for an unauthenticated control request, and 404 for an authenticated unknown run.
- The final Docker image reports no broken Python requirements.

## Closed Critical and High findings

- Stale worker interference: closed with generation-specific workspaces/checkpoints, fenced publication, heartbeat renewal, and exact-generation container cancellation.
- Crash recovery after a partial generation: closed by discarding the dirty generation and rebuilding from a clean source snapshot under a new generation.
- Cancellation stuck at `cancel_requested`: closed with a cancellation reaper and durable `cancelled` terminal state.
- Docker path not proven: closed with two successful live API-to-Docker runs; final evidence uses the final pinned digest.
- Weak baseline oracle: closed with real login, student-list, and class-list HTTP behavior checks against a temporary database.
- Unauthenticated local API: closed with loopback enforcement, TrustedHost middleware, and Bearer authentication.
- Unbounded Docker output/writable host output directory: closed with independent 5 MiB stdout/stderr limits and removal of the writable host bind.

## Remaining non-blocking limitations

- The default provider is intentionally deterministic; a real external LLM provider remains a later iteration.
- The host-wide Python installation has unrelated pre-existing dependency conflicts involving Streamlit/protobuf and pyasn1-modules/pyasn1. The project test suite and isolated Docker runner are unaffected.
- Host `requirements.lock` pins direct dependencies but is not a fully hash-locked transitive lock; the Docker runner dependency set is fully version-pinned and its image digest is frozen.
- Windows cannot exercise the privileged symlink creation test on this host, and its case-insensitive filesystem skips the case-collision test; the corresponding deterministic rejection logic is otherwise covered.
