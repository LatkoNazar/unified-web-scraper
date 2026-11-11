"""Local API and supervised live runs sharing the CLI pipeline and dataset."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from unified_web_parser.core.config import load_job
from unified_web_parser.core.exceptions import ConfigurationError, PipelineError
from unified_web_parser.interfaces.live import (
    LiveRunManager,
    RunCapacityError,
    RunRequest,
    requested_job,
)
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage import DatasetStore
from unified_web_parser.storage.run_lock import RunLockedError


def create_app(
    data_dir: Path = Path("data"),
    config_dir: Path = Path("configs/jobs"),
    *,
    max_active: int = 1,
    runner_factory: Any = None,
) -> FastAPI:
    store = DatasetStore(Path(data_dir))
    config_dir = Path(config_dir).resolve()
    manager = LiveRunManager(
        data_dir, max_active=max_active, runner_factory=runner_factory or PipelineRunner
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await manager.close()

    app = FastAPI(title="Unified Web Parser", version="0.2.0", lifespan=lifespan)
    app.state.run_manager = manager
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(static_dir / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/jobs")
    def jobs():
        return [{"id": path.stem} for path in sorted(config_dir.glob("*.yaml"))]

    @app.post("/runs", status_code=202)
    async def start_run(body: RunRequest):
        try:
            job = requested_job(body, config_dir)
            snapshot = manager.submit(job, body.query)
        except ConfigurationError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RunCapacityError as exc:
            raise HTTPException(429, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "Unable to save run state") from exc
        return JSONResponse(
            snapshot, status_code=202, headers={"Location": f"/runs/{snapshot['run_id']}"}
        )

    @app.get("/runs/{run_id}")
    async def run_snapshot(run_id: str):
        try:
            return manager.snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc
        except (PipelineError, ValueError) as exc:
            raise HTTPException(409, "Run state cannot be restored safely") from exc

    @app.get("/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, last_event_id: str | None = Header(None)):
        try:
            manager.snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc
        except (PipelineError, ValueError) as exc:
            raise HTTPException(409, "Run state cannot be restored safely") from exc

        async def stream():
            # Every connection receives the latest full state, including reconnects.
            async for event in manager.events(run_id):
                if await request.is_disconnected():
                    break
                yield event

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str):
        try:
            snapshot = manager.resume(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc
        except RunCapacityError as exc:
            raise HTTPException(429, str(exc)) from exc
        except (RunLockedError, PipelineError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "Unable to restore the run checkpoint") from exc
        return JSONResponse(
            snapshot, status_code=202, headers={"Location": f"/runs/{snapshot['run_id']}"}
        )

    @app.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str):
        try:
            return manager.cancel(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc

    @app.post("/runs/{run_id}/update", status_code=202)
    async def update_run(run_id: str):
        try:
            snapshot = await manager.update(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc
        except RunCapacityError as exc:
            raise HTTPException(429, str(exc)) from exc
        except (RunLockedError, PipelineError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, "Unable to create the incremental run") from exc
        return JSONResponse(
            snapshot, status_code=202, headers={"Location": f"/runs/{snapshot['run_id']}"}
        )

    @app.post("/jobs/{job_id}/run")
    async def run_job(job_id: str):
        path = (config_dir / f"{job_id}.yaml").resolve()
        if path.parent != config_dir or not path.is_file():
            raise HTTPException(404, "Job not found")
        try:
            job = load_job(path)
            snapshot = manager.submit(job, job.config.search or job.config.name)
        except ConfigurationError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RunCapacityError as exc:
            raise HTTPException(429, str(exc)) from exc
        await manager.wait(snapshot["run_id"])
        final = manager.snapshot(snapshot["run_id"])
        return final["report"] or final

    @app.get("/runs")
    async def runs(limit: int = Query(20, ge=1, le=1000)):
        rows = {item["run_id"]: item for item in store.list_runs(limit=limit)}
        for item in manager.recent(limit):
            rows[item["run_id"]] = {
                **rows.get(item["run_id"], {}),
                **item,
                "summary": item.get("report"),
            }
        return sorted(rows.values(), key=lambda item: item["started_at"], reverse=True)[:limit]

    @app.get("/records")
    def records(job: str | None = None, limit: int = Query(100, ge=1, le=10000)):
        return store.list_records(job_name=job, limit=limit)

    @app.get("/records/{record_id}/history")
    def history(record_id: str, job: str | None = None):
        return store.list_history(record_id, job_name=job)

    return app
