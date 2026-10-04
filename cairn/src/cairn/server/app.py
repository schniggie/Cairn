from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from cairn import __version__
from cairn.server import db
from cairn.server.routers import (
    audit,
    ctf,
    dispatch_config,
    engines,
    events,
    export,
    hints,
    http_records,
    intents,
    projects,
    research,
    research_identities,
    research_runtime_status,
    research_source_compare,
    research_sources,
    settings,
    skills,
    verify_controls,
    auth_control,
    auth_deployment,
    auth_events,
    auth_helper_views,
    auth_requests,
)

STATIC_DIR = Path(__file__).parent / "static"
ADMIN_TOKEN = os.environ.get("CAIRN_ADMIN_TOKEN", "")
# Workers still POST their own graph updates. Listing and export stay behind the token
# so one project cannot read another project's flags.
_WORKER_WRITE_SUFFIXES = (
    "/heartbeat",
    "/claim",
    "/release",
    "/conclude",
    "/complete",
    "/intents",
    "/facts",
    "/hints",
    "/fail",
    "/events",
    "/http-records",
)


class AdminTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # CTF holds platform tokens and model keys. Research accepts a host path
        # that the worker bind-mounts. Both stay behind the same admin bearer as
        # project, skill, and engine management.
        protected = (
            path.startswith("/projects")
            or path.startswith("/skills")
            or path.startswith("/engines")
            or path.startswith("/ctf")
            or path.startswith("/api/research")
            or path.startswith("/research")
        )
        if ADMIN_TOKEN and protected:
            if request.method == "POST" and path.startswith("/projects") and any(
                path.endswith(suffix) for suffix in _WORKER_WRITE_SUFFIXES
            ):
                return await call_next(request)
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if token != ADMIN_TOKEN:
                return Response(status_code=403, content="Forbidden")
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    yield


app = FastAPI(
    title="Cairn",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

if ADMIN_TOKEN:
    app.add_middleware(AdminTokenMiddleware)

app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(skills.router)
app.include_router(engines.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)
app.include_router(audit.router)
app.include_router(events.router)
app.include_router(http_records.router)
app.include_router(dispatch_config.router)
app.include_router(ctf.router)
app.include_router(research.router)
app.include_router(research_runtime_status.router)
app.include_router(research_identities.router)
app.include_router(research_sources.router)
app.include_router(research_source_compare.router)
app.include_router(verify_controls.router)
app.include_router(auth_requests.router)
app.include_router(auth_events.router)
app.include_router(auth_control.router)
app.include_router(auth_deployment.router)
app.include_router(auth_helper_views.router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/research", include_in_schema=False)
@app.get("/research/", include_in_schema=False)
def research_index():
    return FileResponse(STATIC_DIR / "research" / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
