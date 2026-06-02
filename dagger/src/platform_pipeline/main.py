"""
Platform Engineering Pipeline - Dagger v0.21.0
Full pipeline: runtime validation → tests → docker build → push → deploy
"""

import dagger
from dagger import dag, function, object_type, Secret


@object_type
class PlatformPipeline:

    # ─────────────────────────────────────────────
    # Runtime Validation
    # ─────────────────────────────────────────────

    @function
    async def backend_runtime(self) -> str:
        return await (
            dag.container()
            .from_("python:3.10")
            .with_exec(["python", "--version"])
            .stdout()
        )

    @function
    async def frontend_runtime(self) -> str:
        return await (
            dag.container()
            .from_("node:20")
            .with_exec(["node", "--version"])
            .stdout()
        )

    # ─────────────────────────────────────────────
    # Backend Tests
    # ─────────────────────────────────────────────

    @function
    async def backend_test(self) -> str:
        source = dag.current_workspace().directory("../")
        return await (
            dag.container()
            .from_("python:3.10")
            .with_directory("/src", source)
            .with_env_variable("POSTGRES_SERVER", "host.docker.internal")
            .with_workdir("/src/backend")
            .with_exec(["pip", "install", "uv"])
            .with_exec(["uv", "sync"])
            .with_exec(["uv", "run", "pytest", "tests", "-q"])
            .stdout()
        )

    # ─────────────────────────────────────────────
    # Backend Docker Image Build
    # ─────────────────────────────────────────────

    @function
    async def backend_image_build(
        self,
        registry: str = "",
        image_name: str = "backend",
        tag: str = "latest",
        registry_username: Secret | None = None,
        registry_password: Secret | None = None,
    ) -> str:
        source = dag.current_workspace().directory("../")
        uv_image = dag.container().from_("ghcr.io/astral-sh/uv:0.9.26")

        image = (
            dag.container()
            .from_("python:3.10")
            .with_file("/bin/uv",  uv_image.file("/uv"))
            .with_file("/bin/uvx", uv_image.file("/uvx"))
            .with_env_variable("PYTHONUNBUFFERED",    "1")
            .with_env_variable("UV_COMPILE_BYTECODE", "1")
            .with_env_variable("UV_LINK_MODE",        "copy")
            .with_env_variable(
                "PATH",
                "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
            )
            .with_workdir("/app/")
            .with_file("/app/uv.lock",       source.file("uv.lock"))
            .with_file("/app/pyproject.toml", source.file("pyproject.toml"))
            .with_exec([
                "uv", "sync",
                "--frozen",
                "--no-install-workspace",
                "--package", "app",
            ])
            .with_directory("/app/backend/scripts",   source.directory("backend/scripts"))
            .with_file("/app/backend/pyproject.toml", source.file("backend/pyproject.toml"))
            .with_file("/app/backend/alembic.ini",    source.file("backend/alembic.ini"))
            .with_directory("/app/backend/app",       source.directory("backend/app"))
            .with_directory("/app/backend/tests",     source.directory("backend/tests"))
            .with_exec([
                "uv", "sync",
                "--frozen",
                "--package", "app",
            ])
            .with_workdir("/app/backend/")
            .with_default_args(["fastapi", "run", "--workers", "4", "app/main.py"])
        )

        # ── Push to registry if credentials provided ──
        if registry and registry_username and registry_password:
            image_ref = f"{registry}/{image_name}:{tag}"
            pushed = await (
                image
                .with_registry_auth(
                    registry,
                    await registry_username.plaintext(),
                    registry_password,
                )
                .publish(image_ref)
            )
            return f"✅ Pushed: {pushed}"

        # ── Local build only (no push) ──
        await image.sync()
        return "✅ Backend Docker Image Build Successful (local)"

    # ─────────────────────────────────────────────
    # Frontend Docker Image Build
    # ─────────────────────────────────────────────

    @function
    async def frontend_image_build(
        self,
        registry: str = "",
        image_name: str = "frontend",
        tag: str = "latest",
        registry_username: Secret | None = None,
        registry_password: Secret | None = None,
        vite_api_url: str = "",
    ) -> str:
        source = dag.current_workspace().directory("../")

        build_stage = (
            dag.container()
            .from_("oven/bun:1")
            .with_workdir("/app")
            .with_file("/app/package.json",          source.file("package.json"))
            .with_file("/app/bun.lock",               source.file("bun.lock"))
            .with_file("/app/frontend/package.json",  source.file("frontend/package.json"))
            .with_workdir("/app/frontend")
            .with_exec(["bun", "install"])
            .with_directory("/app/frontend", source.directory("frontend"))
        )

        if vite_api_url:
            build_stage = build_stage.with_env_variable("VITE_API_URL", vite_api_url)

        build_stage = build_stage.with_exec(["bun", "run", "build"])

        image = (
            dag.container()
            .from_("nginx:1")
            .with_directory(
                "/usr/share/nginx/html",
                build_stage.directory("/app/frontend/dist"),
            )
            .with_file(
                "/etc/nginx/conf.d/default.conf",
                source.file("frontend/nginx.conf"),
            )
            .with_file(
                "/etc/nginx/extra-conf.d/backend-not-found.conf",
                source.file("frontend/nginx-backend-not-found.conf"),
            )
        )

        # ── Push to registry if credentials provided ──
        if registry and registry_username and registry_password:
            image_ref = f"{registry}/{image_name}:{tag}"
            pushed = await (
                image
                .with_registry_auth(
                    registry,
                    await registry_username.plaintext(),
                    registry_password,
                )
                .publish(image_ref)
            )
            return f"✅ Pushed: {pushed}"

        # ── Local build only (no push) ──
        await image.sync()
        return "✅ Frontend Docker Image Build Successful (local)"

    # ─────────────────────────────────────────────
    # Health Checks
    # ─────────────────────────────────────────────

    @function
    async def backend_health_check(self) -> str:
        return await (
            dag.container()
            .from_("curlimages/curl")
            .with_exec([
                "curl", "-sf",
                "http://host.docker.internal:8000/api/v1/utils/health-check/",
            ])
            .stdout()
        )

    @function
    async def frontend_health_check(self) -> str:
        return await (
            dag.container()
            .from_("curlimages/curl")
            .with_exec([
                "curl", "-sI",
                "http://host.docker.internal:5173",
            ])
            .stdout()
        )

    # ─────────────────────────────────────────────
    # Dokploy Deploy
    # FIX: correct REST endpoint /api/application.redeploy
    # ─────────────────────────────────────────────

    @function
    async def dokploy_deploy(
        self,
        dokploy_url: str,
        dokploy_token: Secret,
        application_id: str,
    ) -> str:
        token = await dokploy_token.plaintext()
        result = await (
            dag.container()
            .from_("curlimages/curl")
            .with_exec([
                "curl", "-sf", "-X", "POST",
                # ✅ FIXED: correct REST endpoint (not tRPC)
                f"{dokploy_url}/api/application.redeploy",
                "-H", "Content-Type: application/json",
                "-H", f"x-api-key: {token}",
                "-d", f'{{"applicationId":"{application_id}"}}',
            ])
            .stdout()
        )
        return result or "✅ Deploy triggered (no response body)"

    @function
    async def dokploy_status(
        self,
        dokploy_url: str,
        dokploy_token: Secret,
        application_id: str,
    ) -> str:
        token = await dokploy_token.plaintext()
        return await (
            dag.container()
            .from_("curlimages/curl")
            .with_exec([
                "curl", "-sf",
                f"{dokploy_url}/api/application.one?applicationId={application_id}",
                "-H", f"x-api-key: {token}",
            ])
            .stdout()
        )

    # ─────────────────────────────────────────────
    # Full Platform Pipeline
    # ─────────────────────────────────────────────

    @function
    async def platform_pipeline(
        self,
        registry: str = "",
        org: str = "",
        tag: str = "latest",
        registry_username: Secret | None = None,
        registry_password: Secret | None = None,
        dokploy_url: str = "",
        dokploy_token: Secret | None = None,
        backend_app_id: str = "",
        frontend_app_id: str = "",
        vite_api_url: str = "",
    ) -> str:
        results: list[str] = []

        # ── 1. Runtime Validation ──
        py_ver   = await self.backend_runtime()
        node_ver = await self.frontend_runtime()
        results.append(f"✓ Backend runtime:  {py_ver.strip()}")
        results.append(f"✓ Frontend runtime: {node_ver.strip()}")

        # ── 2. Backend Tests ──
        test_out = await self.backend_test()
        summary  = [l for l in test_out.strip().splitlines() if l.strip()][-1]
        results.append(f"✓ Tests: {summary}")

        # ── 3. Docker Builds ──
        backend_result = await self.backend_image_build(
            registry=registry,
            image_name=f"{org}/backend" if org else "backend",
            tag=tag,
            registry_username=registry_username,
            registry_password=registry_password,
        )
        results.append(f"✓ {backend_result}")

        frontend_result = await self.frontend_image_build(
            registry=registry,
            image_name=f"{org}/frontend" if org else "frontend",
            tag=tag,
            registry_username=registry_username,
            registry_password=registry_password,
            vite_api_url=vite_api_url,
        )
        results.append(f"✓ {frontend_result}")

        # ── 4. Dokploy Deploy ──
        if dokploy_url and dokploy_token:
            if backend_app_id:
                deploy_out = await self.dokploy_deploy(
                    dokploy_url, dokploy_token, backend_app_id
                )
                results.append(f"✓ Backend deploy: {deploy_out[:80]}")
            if frontend_app_id:
                deploy_out = await self.dokploy_deploy(
                    dokploy_url, dokploy_token, frontend_app_id
                )
                results.append(f"✓ Frontend deploy: {deploy_out[:80]}")
        else:
            results.append("⚠ Dokploy deploy skipped (no token/url provided)")

        # ── 5. Health Checks ──
        backend_health  = await self.backend_health_check()
        results.append(f"✓ Backend health:  {backend_health.strip()}")

        frontend_health = await self.frontend_health_check()
        results.append(f"✓ Frontend health: {frontend_health.splitlines()[0]}")

        return "\n".join([
            "",
            "╔══════════════════════════════════════╗",
            "║   PLATFORM PIPELINE — SUCCESS        ║",
            "╚══════════════════════════════════════╝",
            "",
            *results,
            "",
        ])