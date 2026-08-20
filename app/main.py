"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
import tempfile

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import router
from app.background import BackgroundRunWorker
from app.acceptance import AcceptancePackRegistry
from app.config import load_frozen_project_profile
from app.config.llm_settings import LLMSettings
from app.llm.factory import create_llm_provider
from app.runtime import AssistantRuntime, RuntimeConfigurationError
from app.sandbox import (
    DockerRunner,
    RunnerLimits,
    RunnerProfile,
    RunnerRequest,
)


class _UnconfiguredDockerRunner:
    """Fail-closed placeholder until a built image digest is frozen in YAML."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def preflight(self) -> str:
        raise RuntimeConfigurationError(self.reason)

    def run(self, request: RunnerRequest):
        del request
        raise RuntimeConfigurationError(self.reason)


def create_default_runtime() -> AssistantRuntime:
    project_root = Path(__file__).resolve().parents[1]
    profile_path = (
        project_root / "config" / "projects" / "student-management-backend.yaml"
    )
    frozen = load_frozen_project_profile(profile_path)
    if frozen.runner_digest:
        runner = DockerRunner(
            RunnerProfile(
                image=frozen.runner_image,
                expected_digest=frozen.runner_digest,
                commands={
                    "baseline_test": frozen.commands.baseline_test,
                    "acceptance_test": frozen.commands.acceptance_test,
                    **(
                        {"developer_test": frozen.commands.developer_test}
                        if frozen.commands.developer_test
                        else {}
                    ),
                },
                limits=RunnerLimits(
                    timeout_seconds=frozen.limits.timeout_seconds,
                    memory_mb=frozen.limits.memory_mb,
                    cpus=frozen.limits.cpus,
                ),
            )
        )
    else:
        runner = _UnconfiguredDockerRunner(
            "runner_digest is not frozen; build the Docker image and update the project profile"
        )
    runtime_root = Path(
        os.getenv("RUNTIME_ROOT", str(project_root / "runtime"))
    )
    llm_settings = LLMSettings.from_env(dotenv_path=project_root / ".env")
    provider = create_llm_provider(llm_settings)
    acceptance_registry = AcceptancePackRegistry(
        project_root / "config" / "acceptance-packs",
        tests_root=project_root / "tests" / "evals",
        harness_root=project_root / "tests" / "harness",
    )
    return AssistantRuntime(
        runtime_root=runtime_root,
        profile_paths={frozen.project_id: profile_path},
        runner=runner,
        provider=provider,
        llm_settings=llm_settings,
        acceptance_registry=acceptance_registry,
        worker_id="local-worker",
    )


def create_app(
    *,
    runtime: AssistantRuntime | None = None,
    start_worker: bool = True,
    control_token: str | None = None,
) -> FastAPI:
    runtime = runtime or create_default_runtime()
    control_token = control_token or _load_or_create_control_token(runtime.runtime_root)
    worker = BackgroundRunWorker(runtime) if start_worker else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = runtime
        app.state.worker = worker
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()

    application = FastAPI(
        title="Multi-Agent Software Engineering Assistant",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.runtime = runtime
    application.state.worker = worker
    application.state.control_token = control_token
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "::1"],
    )
    application.include_router(router)

    # 同服务极简 Web 控制台(蓝图 Step 7: 不单独创建前端工程)
    web_dir = Path(__file__).resolve().parent / "web"
    if web_dir.is_dir():
        application.mount(
            "/console", StaticFiles(directory=web_dir, html=True), name="console"
        )

        @application.get("/", include_in_schema=False)
        def console_index():
            return FileResponse(web_dir / "index.html")

    @application.get("/health")
    def health():
        # Keep the unauthenticated liveness endpoint deliberately non-sensitive.
        # Detailed project and Docker status remains available to the local
        # authenticated control flow rather than being disclosed here.
        return {"status": "ok"}

    return application


def _load_or_create_control_token(runtime_root: Path) -> str:
    configured = os.getenv("ASSISTANT_CONTROL_TOKEN")
    if configured:
        if len(configured) < 32:
            raise RuntimeConfigurationError(
                "ASSISTANT_CONTROL_TOKEN must contain at least 32 characters"
            )
        return configured

    token_path = runtime_root / "control-token"
    if token_path.exists():
        if token_path.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(token_path)
        ):
            raise RuntimeConfigurationError("control-token must be a regular file")
        token = token_path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise RuntimeConfigurationError("stored control-token is invalid")
        return token

    token = secrets.token_urlsafe(32)
    runtime_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=runtime_root,
        prefix=".control-token-",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, token_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return token


app = create_app()
