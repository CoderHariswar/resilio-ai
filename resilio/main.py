"""FastAPI application entrypoint.

Run with ``uvicorn resilio.main:app --reload`` or ``resilio serve --reload``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from resilio import __version__
from resilio.api.routes import router, ui_router
from resilio.config import Settings, get_settings
from resilio.container import Container, build_container
from resilio.core.metrics import render_metrics
from resilio.core.security import configure_logging

logger = logging.getLogger(__name__)

ContainerFactory = Callable[[Settings], Awaitable[Container]]


def create_app(
    settings: Settings | None = None,
    *,
    container_factory: ContainerFactory | None = None,
    run_worker: bool = True,
) -> FastAPI:
    """Build the ASGI app. ``container_factory`` lets tests inject fakes."""
    settings = settings or get_settings()
    factory = container_factory or build_container

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        container = await factory(settings)
        app.state.container = container
        stop = asyncio.Event()
        worker = asyncio.create_task(container.runner.serve(stop)) if run_worker else None
        logger.info(
            "ResilioAI %s ready (mock_mode=%s, telemetry=%s, remediation=%s, llm=%s)",
            __version__,
            settings.is_mock_mode,
            container.deps.telemetry.name,
            container.deps.remediation.name,
            container.deps.llm.name,
        )
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(worker, timeout=35)
            await container.aclose()

    app = FastAPI(
        title="ResilioAI",
        version=__version__,
        description="Autonomous AI incident responder with human-in-the-loop remediation.",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(ui_router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {"name": "ResilioAI", "version": __version__, "docs": "/docs", "metrics": "/metrics"}

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> JSONResponse:
        """Readiness: every configured provider answers its health check."""
        deps = app.state.container.deps
        providers = {
            "telemetry": deps.telemetry,
            "remediation": deps.remediation,
            "llm": deps.llm,
        }

        async def check(provider: Any) -> bool:
            try:
                return bool(await asyncio.wait_for(provider.healthcheck(), timeout=3))
            except Exception:
                return False

        results = dict(
            zip(
                providers,
                await asyncio.gather(*(check(p) for p in providers.values())),
                strict=True,
            )
        )
        ready = all(results.values())
        return JSONResponse(
            {"status": "ready" if ready else "degraded", "checks": results},
            status_code=200 if ready else 503,
        )

    @app.get("/metrics", tags=["ops"])
    async def metrics() -> Response:
        """Prometheus exposition of ResilioAI's own operational metrics."""
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    return app


app = create_app()
