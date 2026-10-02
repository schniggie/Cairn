from fastapi import APIRouter, Query

from cairn.server import research_source_compare as service

router=APIRouter(prefix='/api/research/sessions',tags=['research-source-comparison'])


@router.get('/{session_id}/source-comparison')
def compare_snapshots(session_id: str,before: str=Query(min_length=1,max_length=100),after: str=Query(min_length=1,max_length=100)):
    return service.compare_snapshots(session_id,before,after)


@router.get('/{session_id}/source-comparison/file')
def compare_file(session_id: str,before: str=Query(min_length=1,max_length=100),after: str=Query(min_length=1,max_length=100),path: str=Query(min_length=1,max_length=4096)):
    return service.compare_file(session_id,before,after,path)
