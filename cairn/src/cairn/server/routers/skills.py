from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from cairn import skills_store
from cairn.server.admin_auth import require_admin
from cairn.server.models import SkillContent, SkillCreate, SkillEnable, SkillInfo

router = APIRouter(tags=["skills"])


def _info(meta: skills_store.SkillMeta) -> SkillInfo:
    return SkillInfo(name=meta.name, description=meta.description, enabled=meta.enabled)


def _find(name: str) -> skills_store.SkillMeta:
    for meta in skills_store.list_skills():
        if meta.name == name:
            return meta
    raise HTTPException(404, f"skill not found: {name}")


@router.get("/skills", response_model=list[SkillInfo])
def list_skills():
    return [_info(meta) for meta in skills_store.list_skills()]


@router.get("/skills/{name}", response_model=SkillContent)
def get_skill(name: str):
    _find(name)
    return SkillContent(name=name, content=skills_store.read_skill_md(name))


@router.post("/skills", status_code=201, response_model=SkillInfo, dependencies=[Depends(require_admin)])
def create_skill(body: SkillCreate):
    try:
        skills_store.create_skill(body.name, body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _info(_find(body.name))


@router.put("/skills/{name}", response_model=SkillInfo, dependencies=[Depends(require_admin)])
def update_skill(name: str, body: SkillContent):
    _find(name)
    skills_store.write_skill_md(name, body.content)
    return _info(_find(name))


@router.put("/skills/{name}/enabled", response_model=SkillInfo, dependencies=[Depends(require_admin)])
def set_enabled(name: str, body: SkillEnable):
    _find(name)
    skills_store.set_enabled(name, body.enabled)
    return _info(_find(name))


@router.delete("/skills/{name}", dependencies=[Depends(require_admin)])
def delete_skill(name: str):
    _find(name)
    skills_store.delete_skill(name)
    return {"deleted": name}


@router.post("/skills/upload", status_code=201, response_model=SkillInfo, dependencies=[Depends(require_admin)])
async def upload_skill(request: Request):
    """Accept a zip archive as the raw request body (one top-level skill directory)."""
    try:
        payload = await _bounded_body(request, skills_store.MAX_UPLOAD_BYTES)
        name = skills_store.import_zip(payload)
    except ValueError as exc:
        status = 413 if "limit" in str(exc) else 400
        raise HTTPException(status, str(exc)) from exc
    return _info(_find(name))


async def _bounded_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError as exc:
            raise HTTPException(400, "invalid content-length") from exc
        if size > limit:
            raise HTTPException(413, "zip archive exceeds the upload size limit")
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > limit:
            raise HTTPException(413, "zip archive exceeds the upload size limit")
        chunks.extend(chunk)
    return bytes(chunks)
