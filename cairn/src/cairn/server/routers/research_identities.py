"""Personal testing-identity metadata routes. Never accept secret values."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from cairn.server import research_identities as store
from cairn.server.routers.research import same_origin_write

router=APIRouter(prefix='/api/research/sessions',tags=['research-identities'],dependencies=[Depends(same_origin_write)])


async def _payload(request,fields):
    try:
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>2048: raise ValueError()
        body=json.loads(raw)
        if not isinstance(body,dict) or set(body)!=set(fields) or not all(isinstance(body[k],str) for k in fields):
            raise ValueError()
        return body
    except (ValueError,TypeError,UnicodeError,RecursionError):
        # Do not include validation inputs in responses: users may mistakenly
        # paste a credential here even though this API accepts filenames only.
        raise HTTPException(422,'请求仅接受身份标签和私有目录中的 JSON 文件名；请勿提交秘密内容') from None


@router.get('/{session_id}/identities')
def list_identities(session_id: str):
    return store.list_identities(session_id)


@router.post('/{session_id}/identities',status_code=201)
async def import_identity(session_id: str,request: Request):
    body=await _payload(request,('label','source_file'))
    return await run_in_threadpool(store.import_identity,session_id,body['label'],body['source_file'])


@router.post('/{session_id}/identities/{identity_id}/replace')
async def replace_identity(session_id: str,identity_id: str,request: Request):
    body=await _payload(request,('source_file',))
    return await run_in_threadpool(store.import_identity,session_id,None,body['source_file'],identity_id)


@router.delete('/{session_id}/identities/{identity_id}')
def revoke_identity(session_id: str,identity_id: str):
    return store.revoke_identity(session_id,identity_id)
