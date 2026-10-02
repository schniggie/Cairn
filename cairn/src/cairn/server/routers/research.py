from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from cairn.server.db import get_conn
from cairn.server.research_models import CreateResearch, ResearchHint, ResearchMaterials, UpdateResearchBudget
from cairn.server import research_services as service


def same_origin_write(request: Request):
    """Personal API shares the app trust boundary; forbid browser cross-site writes.

    Headerless CLI/local fixtures remain usable. This is CSRF prevention, not a
    substitute for deployment authentication when exposing Cairn to other users.
    """
    if request.method in ('GET','HEAD','OPTIONS'):
        return
    if request.headers.get('sec-fetch-site') in ('cross-site','same-site'):
        raise HTTPException(403,'研究操作仅接受当前 Cairn 页面发起的请求')
    origin=request.headers.get('origin')
    if origin:
        parsed=urlsplit(origin)
        if parsed.scheme!=request.url.scheme or parsed.netloc!=request.url.netloc or parsed.path not in ('','/'):
            raise HTTPException(403,'研究操作的来源与当前 Cairn 服务不一致')
    elif request.headers.get('referer'):
        parsed=urlsplit(request.headers['referer'])
        if parsed.scheme!=request.url.scheme or parsed.netloc!=request.url.netloc:
            raise HTTPException(403,'研究操作的来源与当前 Cairn 服务不一致')


router=APIRouter(prefix='/api/research/sessions',tags=['research'],dependencies=[Depends(same_origin_write)])


def validate_repo(repo):
    # Filesystem validation happens before the write transaction. No repository scans.
    if repo:
        path=Path(repo)
        if not path.is_dir(): raise HTTPException(422,'代码目录在 Kali 上不存在或不是目录')
        return str(path.resolve())
    return repo


@router.get('')
def list_research():
    with get_conn() as conn:
        return {'items':service.list_sessions(conn)}


@router.post('',status_code=201)
def create_research(body: CreateResearch):
    body.repo=validate_repo(body.repo)
    with get_conn() as conn:
        return service.create_session(conn,body)


@router.get('/{session_id}')
def get_research(session_id: str):
    with get_conn() as conn:
        return service.get_session(conn,session_id)


@router.get('/{session_id}/events')
def events(session_id: str,after: int=Query(0,ge=0),limit: int=Query(200,ge=1,le=1000)):
    with get_conn() as conn:
        return service.list_events(conn,session_id,after,limit)


@router.post('/{session_id}/pause')
def pause(session_id: str):
    with get_conn() as conn:
        return service.pause_session(conn,session_id)


@router.post('/{session_id}/resume')
def resume(session_id: str):
    with get_conn() as conn:
        return service.resume_session(conn,session_id)


@router.post('/{session_id}/complete')
def complete(session_id: str):
    with get_conn() as conn:
        return service.complete_session(conn,session_id)


@router.post('/{session_id}/hints')
def hints(session_id: str,body: ResearchHint):
    with get_conn() as conn:
        return service.add_hint(conn,session_id,body.content)


@router.patch('/{session_id}/materials')
def materials(session_id: str,body: ResearchMaterials):
    if 'repo' in body.model_fields_set:
        body.repo=validate_repo(body.repo)
    with get_conn() as conn:
        return service.update_materials(conn,session_id,body)


@router.get('/{session_id}/report.md')
def report(session_id: str):
    with get_conn() as conn:
        session=service.get_session(conn,session_id)
    return Response(service.render_report(session),media_type='text/markdown; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{session_id}-research.md"'})


@router.patch('/{session_id}/budget')
def budget(session_id: str,body: UpdateResearchBudget):
    with get_conn() as conn:
        return service.update_budget(conn,session_id,body)


@router.post('/{session_id}/recheck')
def recheck(session_id: str):
    """M4 change trigger: re-fingerprint the target material (repo tree or url probe)
    against the stored baseline; if it changed, re-queue the completed session as a
    re-audit round (same authorization/budget/history, a new run batch)."""
    with get_conn() as conn:        session = service.get_session(conn, session_id)
    scope = service.session_scope(session)
    if not scope or scope == 'objective':
        return {'changed': False, 'first': False, 'requeued': False, 'reason': '无 url/repo 材料可检测变化'}
    fingerprint = None
    if session.get('repo'):
        fingerprint = service.fingerprint_repo(session['repo'])
    elif session.get('url'):
        fingerprint = service.fingerprint_url(session['url'])
    if fingerprint is None:
        return {'changed': False, 'first': False, 'requeued': False, 'reason': '无材料可指纹化'}
    with get_conn() as conn:
        res = service.recheck_scope(conn, scope, fingerprint)
        return service.requeue_for_reaudit(conn, session_id, res['changed'])


@router.get('/{session_id}/reports')
def list_reports(session_id: str, before: str | None = Query(None),
                 limit: int = Query(20, ge=1, le=100)):
    with get_conn() as conn:
        return service.list_reports(conn, session_id, before, limit)


@router.post('/{session_id}/reports',status_code=201)
def create_report(session_id: str):
    with get_conn() as conn:
        return service.create_report(conn,session_id)


@router.get('/{session_id}/reports/{report_id}')
def get_report(session_id: str,report_id: str):
    with get_conn() as conn:
        return service.get_report(conn,session_id,report_id)


@router.get('/{session_id}/reports/{report_id}/report.md')
def saved_report_markdown(session_id: str,report_id: str):
    with get_conn() as conn:
        report=service.get_report(conn,session_id,report_id)
    return Response(report['markdown'],media_type='text/markdown; charset=utf-8',headers={
        'Content-Disposition':f'attachment; filename="{report_id}.md"',
        'ETag':'"'+report['sha256']+'"',
        'X-Content-Type-Options':'nosniff',
    })
