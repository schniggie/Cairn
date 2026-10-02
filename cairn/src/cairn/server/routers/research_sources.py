from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from cairn.server import research_sources as store
from cairn.server.routers.research import same_origin_write

router=APIRouter(prefix='/api/research/sessions',tags=['research-sources'],dependencies=[Depends(same_origin_write)])


@router.post('/{session_id}/sources',status_code=201)
def create_snapshot(session_id: str):
    return store.create_snapshot(session_id)


@router.get('/{session_id}/sources')
def list_snapshots(session_id: str):
    return store.list_snapshots(session_id)


@router.get('/{session_id}/sources/{snapshot_id}')
def get_snapshot(session_id: str,snapshot_id: str):
    return store.get_snapshot(session_id,snapshot_id)


@router.get('/{session_id}/sources/{snapshot_id}/file')
def get_file(session_id: str,snapshot_id: str,path: str=Query(min_length=1,max_length=4096)):
    return store.get_file(session_id,snapshot_id,path)


class SourceSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    path: str=Field(min_length=1,max_length=4096)
    start_line: int=Field(ge=1,strict=True)
    end_line: int=Field(ge=1,strict=True)


@router.post('/{session_id}/sources/{snapshot_id}/evidence',status_code=201)
def preserve_lines(session_id: str,snapshot_id: str,body: SourceSelection):
    return store.preserve_lines(session_id,snapshot_id,body.path,body.start_line,body.end_line)
