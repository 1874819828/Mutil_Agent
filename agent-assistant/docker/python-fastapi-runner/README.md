# Python/FastAPI test runner

Build while network access is available:

```powershell
docker build -t ai-agent/python-fastapi-runner:2026-08-11 .
docker image inspect ai-agent/python-fastapi-runner:2026-08-11 --format '{{index .RepoDigests 0}}'
```

Runtime execution is performed with all capabilities dropped, a read-only root
filesystem, a non-root UID, finite resource limits, and private temporary
mounts. The application must fail closed when this image is unavailable.

Baseline, syntax, and developer tests run in a single container with networking
disabled. Acceptance tests use a two-container black-box topology:

- the SUT container receives the read-only generated workspace and an ephemeral
  database volume, but never receives the acceptance-test mount;
- the driver container receives the acceptance tests read-only, but never the
  workspace;
- both containers join a generation-scoped `docker network create --internal`
  network and the driver calls `http://sut:8000`;
- neither container publishes a host port, and containers, network, and volume
  are removed at the end of the run or on cancellation.

Acceptance suites therefore need to treat `SUT_BASE_URL` as their only SUT
interface. They must not import application modules or read `/workspace`.
