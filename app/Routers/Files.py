"""
File intake endpoint (service-to-service / trusted callers only).

Accepts an uploaded file, saves it on THIS server, then FORWARDS it over HTTP to the
independent file-processor service (`FILE_PROCESSOR_URL` + service token). No shared
filesystem. Registers a `queued` JobStatus row keyed by a generated job_id and returns
it so the client can poll (GET /status/{job_id}) or watch live (GET /status/stream).
"""
import asyncio
import shutil
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Request, Response, UploadFile, File, Form, Depends, status
from sqlalchemy.dialects.postgresql import insert as pg_insert

from settings import Router, UPLOAD_PATH, FILE_PROCESSOR_URL, FILE_PROCESSOR_TOKEN
from Database import JobStatus
from Utils import success_response, warning_response, error_response
from service_auth import verify_service_token

router = Router(prefix="/files", tags=["Files"], dependencies=[Depends(verify_service_token)])

# Must match the file-processor PROCESSORS keys.
_ALLOWED_KINDS = {"json", "csv", "excel", "cirium"}


def _save_sync(upload_file, dest: Path) -> None:
    """Stream the upload to disk (run in a thread to keep the event loop free)."""
    with dest.open("wb") as out:
        shutil.copyfileobj(upload_file, out)


async def _set_status(request: Request, job_id: str, ref: str, state: str, message: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    values = {"job_id": job_id, "kind": "file", "ref": ref, "state": state, "updated_at": now}
    if message is not None:
        values["message"] = message
    if state in ("success", "error", "skipped"):
        values["finished_at"] = now
    stmt = pg_insert(JobStatus).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["job_id"],
        set_={k: stmt.excluded[k] for k in values if k != "job_id"},
    )
    async with request.app.state.db_client.session("service") as session:
        await session.execute(stmt)


@router.post("")
async def upload_file(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    kind: str = Form(...),
):
    if kind not in _ALLOWED_KINDS:
        return warning_response(
            request=request, response=response,
            msg=f"Unknown kind '{kind}'. Allowed: {sorted(_ALLOWED_KINDS)}",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Sanitize the client-supplied filename to a bare basename (no traversal / absolute paths).
    safe_name = Path(file.filename or "").name
    if not safe_name:
        return warning_response(
            request=request, response=response,
            msg="Missing or invalid filename",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # 1) save on this server
    UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
    job_id = _uuid.uuid4().hex
    dest = UPLOAD_PATH / f"{job_id}__{safe_name}"
    await asyncio.to_thread(_save_sync, file.file, dest)
    await _set_status(request, job_id, str(dest), "queued")

    # 2) forward to the file-processor service (server-to-server, streamed from disk)
    try:
        with dest.open("rb") as fh:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    FILE_PROCESSOR_URL.rstrip("/") + "/process",
                    headers={"X-Service-Token": FILE_PROCESSOR_TOKEN},
                    data={"kind": kind, "job_id": job_id},
                    files={"file": (safe_name, fh)},
                )
        resp.raise_for_status()
    except Exception as e:
        await _set_status(request, job_id, str(dest), "error", message=f"forward to file-processor failed: {e}")
        return error_response(
            request=request, response=response,
            msg=f"file-processor unavailable: {e}",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    # file-processor now owns its own copy — drop core-api's staged copy (avoid disk growth)
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        pass

    return success_response(
        request=request, response=response,
        data={"job_id": job_id, "kind": kind, "filename": safe_name},
        msg="File accepted and forwarded for processing",
        status_code=status.HTTP_202_ACCEPTED,
    )
