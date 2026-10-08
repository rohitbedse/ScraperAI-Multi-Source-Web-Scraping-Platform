import asyncio
import json
import re
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app import config
from app.core.job_manager import TERMINAL, manager
from app.db.models import Job, JobError, JobEvent
from app.db.session import session_scope
from app.schemas.jobs import JobCreate, JobOut
from app.scrapers.registry import describe_sources

JOB_ID = Path(pattern=r"^[0-9a-f]{32}$")


def require_api_key(x_api_key: Optional[str] = Header(None), api_key: Optional[str] = Query(None)):
    """Optional auth: only enforced when API_KEY is set. Query form exists for EventSource/links."""
    if config.API_KEY and not secrets.compare_digest(x_api_key or api_key or "", config.API_KEY):
        raise HTTPException(401, "Invalid or missing API key")


public = APIRouter()
router = APIRouter(dependencies=[Depends(require_api_key)])


@public.get("/health")
def health():
    return {"status": "ok"}


@router.get("/sources")
def sources():
    return describe_sources()


def _job_or_404(s, job_id: str) -> Job:
    j = s.get(Job, job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return j


def _out(s, j: Job, traceback: bool = False) -> JobOut:
    errs = s.scalars(select(JobError).where(JobError.job_id == j.id).order_by(JobError.id)).all()
    return JobOut.from_db(j, errs, with_traceback=traceback)


@router.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(body: JobCreate):
    try:
        job_id = await run_in_threadpool(manager.create_job, body.source, body.params)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{body.source}'")
    except ValidationError as e:
        raise HTTPException(422, [{"field": ".".join(map(str, x["loc"])), "msg": x["msg"]} for x in e.errors()])
    return await _get(job_id)


async def _get(job_id: str, traceback: bool = False) -> JobOut:
    def q():
        with session_scope() as s:
            return _out(s, _job_or_404(s, job_id), traceback)
    return await run_in_threadpool(q)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(source: Optional[str] = None, status: Optional[str] = None,
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    with session_scope() as s:
        q = select(Job).order_by(Job.created_at.desc()).limit(limit).offset(offset)
        if source:
            q = q.where(Job.source == source)
        if status:
            q = q.where(Job.status == status)
        return [JobOut.from_db(j) for j in s.scalars(q)]


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str = JOB_ID, include_traceback: bool = False):
    return await _get(job_id, include_traceback)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str = JOB_ID):
    await _get(job_id)
    if not await run_in_threadpool(manager.cancel, job_id):
        raise HTTPException(409, "Job is already finished")
    return await _get(job_id)


@router.post("/jobs/{job_id}/rerun", response_model=JobOut, status_code=201)
async def rerun_job(job_id: str = JOB_ID):
    old = await _get(job_id)
    if old.status in ("queued", "running", "cancelling"):
        raise HTTPException(409, "Job is still active")
    new_id = await run_in_threadpool(manager.create_job, old.source, old.params)
    return await _get(new_id)


@router.get("/jobs/{job_id}/download")
def download(job_id: str = JOB_ID):
    with session_scope() as s:
        j = _job_or_404(s, job_id)
        name = j.output_path
    if not name:
        raise HTTPException(404, "No output available for this job (it may have expired)")
    base = (config.job_dir(job_id) / "output").resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or not path.is_file():   # no traversal
        raise HTTPException(404, "Output file not found")
    return FileResponse(path, media_type="application/json", filename=f"{j.source}_{job_id[:8]}.json")


def _fetch_events(job_id: str, after: int):
    with session_scope() as s:
        rows = s.execute(select(JobEvent.id, JobEvent.event_type, JobEvent.payload)
                         .where(JobEvent.job_id == job_id, JobEvent.id > after)
                         .order_by(JobEvent.id).limit(500)).all()
        status = s.scalar(select(Job.status).where(Job.id == job_id))
    return rows, status


@router.get("/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str = JOB_ID,
                     last_event_id: Optional[str] = Header(None),
                     after: int = Query(0, ge=0)):
    """SSE. Replays persisted events after Last-Event-ID (or ?after=), then streams live ones."""
    await _get(job_id)
    try:
        cursor = int(last_event_id) if last_event_id else after
    except ValueError:
        cursor = after

    async def gen():
        nonlocal cursor
        idle = 0
        yield "retry: 2000\n\n"
        while True:
            rows, status = await run_in_threadpool(_fetch_events, job_id, cursor)
            for eid, etype, payload in rows:
                cursor = eid
                yield f"id: {eid}\nevent: {etype}\ndata: {payload}\n\n"
            if not rows:
                if status in TERMINAL:
                    yield "event: end\ndata: {}\n\n"
                    return
                idle += 1
                if idle % 30 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)
            if await request.is_disconnected():
                return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
