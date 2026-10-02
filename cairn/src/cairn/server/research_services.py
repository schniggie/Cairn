"""Transactional research primitives. Callers own commit/rollback and never hold a
write transaction while invoking a model, network request, or filesystem scan.
"""
from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.services import next_project_id, next_hint_id, next_intent_id, next_fact_id, utcnow


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _id(prefix):
    return prefix + '-' + uuid4().hex


def _lock(conn, session_id):
    if not conn.execute('UPDATE research_sessions SET id=id WHERE id=?', (session_id,)).rowcount:
        raise HTTPException(404, '研究不存在')


def _row(conn, session_id):
    row = conn.execute('SELECT * FROM research_sessions WHERE id=?', (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '研究不存在')
    return row


def _decode(row):
    result = dict(row)
    result.pop('session_id', None)
    for key in list(result):
        if key.endswith('_json'):
            value = result.pop(key)
            result[key[:-5]] = json.loads(value) if value is not None else None
    return result


def _summary(conn, row):
    result = _decode(row)
    result['title'] = conn.execute('SELECT title FROM projects WHERE id=?', (row['id'],)).fetchone()[0]
    result['stage'] = result['phase']
    result['next'] = result['next_direction']
    return result


def worker_heartbeat(conn, worker_id, hostname, state='idle', current_session_id=None):
    """Register/refresh the running research worker's liveness marker."""
    now = utcnow()
    existing = conn.execute(
        'SELECT worker_id FROM research_worker_runtime WHERE worker_id=?', (worker_id,)
    ).fetchone()
    if existing:
        conn.execute(
            'UPDATE research_worker_runtime SET hostname=?,last_heartbeat_at=?,state=?,current_session_id=? WHERE worker_id=?',
            (hostname, now, state, current_session_id, worker_id),
        )
    else:
        conn.execute(
            'INSERT INTO research_worker_runtime (worker_id,hostname,started_at,last_heartbeat_at,state,current_session_id) VALUES (?,?,?,?,?,?)',
            (worker_id, hostname, now, now, state, current_session_id),
        )


def read_worker_runtime(conn):
    """Return the single live worker marker (most recently seen worker)."""
    rows = conn.execute(
        'SELECT * FROM research_worker_runtime ORDER BY last_heartbeat_at DESC,worker_id LIMIT 1'
    ).fetchone()
    if rows is not None:
        result = dict(rows)
        result.pop('hostname', None)
        return result
    return None


def list_sessions(conn):
    rows = conn.execute('SELECT * FROM research_sessions ORDER BY updated_at DESC,id DESC').fetchall()
    keys = ('id','title','objective','url','repo','mode','status','stage','next','stack','budget','usage','created_at','updated_at','latest_error')
    return [{k: s[k] for k in keys} for s in (_summary(conn,row) for row in rows)]


def list_events(conn, session_id, after=0, limit=200):
    _row(conn,session_id)
    rows = conn.execute('SELECT * FROM research_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?', (session_id,after,limit)).fetchall()
    items = [_decode(row) for row in rows]
    return {'items':items, 'next_cursor':items[-1]['seq'] if items else after}


def get_session(conn, session_id):
    # A detail consists of several SELECTs. Hold one WAL read snapshot until the
    # caller commits; do not replace an existing write transaction.
    if not conn.in_transaction:
        conn.execute('BEGIN')
    result = _summary(conn,_row(conn,session_id))
    for key, table in (('events','research_events'),('assets','research_assets'),('evidence','research_evidence'),('findings','research_findings')):
        order = 'seq' if key == 'events' else ('created_at,id' if key == 'findings' else 'time,id')
        result[key] = [_decode(row) for row in conn.execute(f'SELECT * FROM {table} WHERE session_id=? ORDER BY {order}',(session_id,))]
    return result


def append_event(conn, session_id, kind, title, description, detail=None, evidence_ids=None, finding_id=None):
    _lock(conn,session_id)
    now = utcnow()
    conn.execute('UPDATE research_sessions SET event_seq=event_seq+1,updated_at=? WHERE id=?',(now,session_id))
    seq = _row(conn,session_id)['event_seq']
    eid = _id('event')
    conn.execute('INSERT INTO research_events (id,session_id,seq,kind,title,description,time,detail_json,evidence_ids_json,finding_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (eid,session_id,seq,kind,title,description,now,_json(detail) if detail is not None else None,_json(evidence_ids or []),finding_id))
    return _decode(conn.execute('SELECT * FROM research_events WHERE id=?',(eid,)).fetchone())


def record_asset(conn,session_id,kind,value,source='research',metadata=None):
    _lock(conn,session_id)
    conn.execute('INSERT OR IGNORE INTO research_assets (id,session_id,kind,value,source,time,metadata_json) VALUES (?,?,?,?,?,?,?)',
        (_id('asset'),session_id,kind,value,source,utcnow(),_json(metadata or {})))
    return _decode(conn.execute('SELECT * FROM research_assets WHERE session_id=? AND kind=? AND value=?',(session_id,kind,value)).fetchone())


def _authorization(url,repo,revision,budget):
    return {'revision':revision,'confirmed_at':utcnow(),'url':url,'repo':repo,'budget':budget,
        'allowed_actions':['read_authorized_code','request_authorized_target','run_workspace_scripts'],
        'policy':'personal_project_owner'}


def create_session(conn, body):
    if not body.authorization_confirmed:
        raise HTTPException(403,'请确认本项目的研究范围与预算')
    pid, now = next_project_id(conn), utcnow()
    conn.execute("INSERT INTO projects (id,title,status,bootstrap_enabled,project_kind,created_at) VALUES (?,?,'active',0,'research',?)",(pid,body.title,now))
    origin = '网站：' + (body.url or '未提供') + '；代码：' + (body.repo or '未提供')
    conn.executemany('INSERT INTO facts (id,project_id,description) VALUES (?,?,?)',[('origin',pid,origin),('goal',pid,body.objective)])
    iid=next_intent_id(conn,pid)
    conn.execute('INSERT INTO intents (id,project_id,description,creator,created_at) VALUES (?,?,?,?,?)',(iid,pid,body.objective,'research-owner',now))
    conn.execute('INSERT INTO intent_sources (intent_id,project_id,fact_id) VALUES (?,?,?)',(iid,pid,'origin'))
    budget=body.budget.model_dump()
    authorization=_authorization(body.url,body.repo,1,budget)
    mode='combined' if body.url and body.repo else ('web' if body.url else 'code')
    conn.execute('INSERT INTO research_sessions (id,objective,url,repo,mode,status,authorization_json,budget_json,usage_json,next_direction,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (pid,body.objective,body.url,body.repo,mode,'queued',_json(authorization),_json(budget),_json({'elapsed_seconds':0,'requests':0,'cost_usd':0,'steps':0}),'确认材料与可用研究环境',now,now))
    conn.execute('INSERT INTO research_authorizations VALUES (?,?,?,?)',(pid,1,_json(authorization),now))
    for kind,value in (('url',body.url),('repo',body.repo)):
        if value: record_asset(conn,pid,kind,value,'user')
    append_event(conn,pid,'authorization','项目授权已确认','材料与预算已保存；后续暂停和恢复沿用此授权。',authorization)
    append_event(conn,pid,'queued','等待研究执行器','研究任务已持久保存，等待后台认领。')
    return get_session(conn,pid)


def pause_session(conn,session_id):
    _lock(conn,session_id)
    row=_row(conn,session_id)
    if row['status'] in ('paused','pause_requested','completed'): return get_session(conn,session_id)
    status='pause_requested' if row['status']=='running' else 'paused'
    conn.execute('UPDATE research_sessions SET status=?,updated_at=? WHERE id=?',(status,utcnow(),session_id))
    append_event(conn,session_id,'pause','暂停请求已接收' if status=='pause_requested' else '研究已暂停',
        '执行器将在停止当前工作后确认暂停。' if status=='pause_requested' else '研究尚未运行，已停止排队。')
    return get_session(conn,session_id)


def resume_session(conn,session_id):
    _lock(conn,session_id)
    row=_row(conn,session_id)
    if row['status'] in ('queued','running'): return get_session(conn,session_id)
    if row['status']=='pause_requested': raise HTTPException(409,'正在停止执行，请等待已暂停后继续')
    usage,budget=json.loads(row['usage_json']),json.loads(row['budget_json'])
    if any(usage.get(k,0)>=v for k,v in (('elapsed_seconds',budget['minutes']*60),('requests',budget['requests']),('steps',budget['max_steps']),('cost_usd',budget['max_cost_usd']))):
        raise HTTPException(409,'预算已耗尽，恢复不会重置预算；需要明确追加预算')
    if row['cost_pending_check']:
        raise HTTPException(409,'存在未核对的研究费用，恢复会沿用累计预算；需先确认费用待核对项后再继续')
    conn.execute("UPDATE research_sessions SET status='queued',latest_error=NULL,phase=CASE WHEN phase=3 THEN 1 ELSE phase END,updated_at=? WHERE id=?",(utcnow(),session_id))
    conn.execute("UPDATE projects SET status='active' WHERE id=?",(session_id,))
    append_event(conn,session_id,'resume','研究已重新排队','保留研究历史、已用预算和原项目授权。')
    return get_session(conn,session_id)


def complete_session(conn,session_id):
    _lock(conn,session_id)
    row=_row(conn,session_id)
    if row['status']=='completed': return get_session(conn,session_id)
    if row['status'] in ('running','pause_requested'): raise HTTPException(409,'请先暂停并等待执行器停止后收束研究')
    conn.execute("UPDATE research_sessions SET status='completed',phase=3,updated_at=? WHERE id=?",(utcnow(),session_id))
    conn.execute("UPDATE projects SET status='completed' WHERE id=?",(session_id,))
    append_event(conn,session_id,'completed','本轮研究已收束','由用户结束；尚未验证的事项仍保留为未验证，不代表目标安全。')
    return get_session(conn,session_id)


def add_hint(conn,session_id,content):
    _lock(conn,session_id)
    hid=next_hint_id(conn,session_id)
    conn.execute('INSERT INTO hints (id,project_id,content,creator,created_at) VALUES (?,?,?,?,?)',(hid,session_id,content,'human',utcnow()))
    conn.execute('UPDATE research_sessions SET next_direction=? WHERE id=?',(content,session_id))
    append_event(conn,session_id,'hint','已接收补充方向',content,{'hint_id':hid})
    return get_session(conn,session_id)


def update_materials(conn,session_id,body):
    _lock(conn,session_id)
    row=_row(conn,session_id)
    url=body.url if 'url' in body.model_fields_set else row['url']
    repo=body.repo if 'repo' in body.model_fields_set else row['repo']
    if not url and not repo: raise HTTPException(422,'至少保留一种研究材料')
    if url==row['url'] and repo==row['repo']: return get_session(conn,session_id)
    if row['status'] in ('running','pause_requested'): raise HTTPException(409,'请先暂停研究并等待执行器停止，再变更材料')
    if row['status']=='completed': raise HTTPException(409,'已收束研究的材料快照不可变更')
    added=(url and url!=row['url']) or (repo and repo!=row['repo'])
    if added and not body.authorization_confirmed: raise HTTPException(403,'新增或更换目标需要确认本次范围变更')
    rev=row['authorization_revision']+1
    auth=_authorization(url,repo,rev,json.loads(row['budget_json']))
    if not added:
        auth['confirmed_at']=json.loads(row['authorization_json'])['confirmed_at']
    mode='combined' if url and repo else ('web' if url else 'code')
    # Preserve old asset/evidence records; the current authorization is the execution boundary.
    conn.execute("UPDATE research_sessions SET url=?,repo=?,mode=?,authorization_revision=?,authorization_json=?,worker_session_id=NULL,cursor_json='{}',phase=0 WHERE id=?",(url,repo,mode,rev,_json(auth),session_id))
    conn.execute('INSERT INTO research_authorizations VALUES (?,?,?,?)',(session_id,rev,_json(auth),utcnow()))
    for kind,value in (('url',url),('repo',repo)):
        if value: record_asset(conn,session_id,kind,value,'user')
    append_event(conn,session_id,'materials','研究材料已更新','保留历史证据；后续执行使用当前授权材料。',auth)
    return get_session(conn,session_id)


def claim_session(conn,worker_id,lease_seconds=60):
    # Acquire SQLite writer before selection, preventing two workers claiming a session.
    conn.execute('UPDATE research_sessions SET id=id WHERE 0')
    row=conn.execute("SELECT id FROM research_sessions WHERE status='queued' AND lease_owner IS NULL ORDER BY created_at,id LIMIT 1").fetchone()
    if not row: return None
    expiry=(datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')
    conn.execute("UPDATE research_sessions SET status='running',lease_owner=?,lease_expires_at=?,latest_error=NULL,updated_at=? WHERE id=?",(worker_id,expiry,utcnow(),row['id']))
    append_event(conn,row['id'],'running','研究执行器已接入','正在检查材料、执行边界与现有检查点。')
    # Each claim is one independent run batch: a fresh ledger row for attribution,
    # settlement and dedup. A worker that loses the lease later may still file THIS
    # batch's consumption but can never write results into the re-claimed session.
    create_run_account(conn, row['id'], worker_id)
    return get_session(conn,row['id'])


def heartbeat(conn,session_id,worker_id,lease_seconds=60):
    expiry=(datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return bool(conn.execute("UPDATE research_sessions SET lease_expires_at=? WHERE id=? AND lease_owner=? AND status IN ('running','pause_requested')",(expiry,session_id,worker_id)).rowcount)


def finish_run(conn,session_id,worker_id,status,latest_error=None):
    if status not in ('queued','paused','waiting_input','completed','failed'): raise ValueError('Invalid terminal run status')
    _lock(conn,session_id)
    row=_row(conn,session_id)
    if row['lease_owner']!=worker_id: return False
    if row['status']=='pause_requested': status='paused'
    conn.execute("UPDATE research_sessions SET status=?,lease_owner=NULL,lease_expires_at=NULL,latest_error=?,updated_at=?,phase=CASE WHEN ?='completed' THEN 3 ELSE phase END WHERE id=?",(status,latest_error,utcnow(),status,session_id))
    if status=='completed': conn.execute("UPDATE projects SET status='completed' WHERE id=?",(session_id,))
    append_event(conn,session_id,status,{'queued':'等待下一轮研究','paused':'研究已暂停','waiting_input':'研究需要补充条件','completed':'本轮研究已完成','failed':'本轮执行失败'}[status],latest_error or '执行器已保存检查点并释放任务。')
    return True


def classify_failure(*, returncode=None, timed_out=False, cancelled=False, detail="") -> dict:
    """M4: classify why a run step failed into (category, recoverable, reason).
    Only clearly-transient provider/rate-limit and timeout cases are recoverable so
    bounded auto-recovery never loops on a real bug, a budget shortfall, or a
    protocol violation."""
    d = (detail or "").lower()
    if returncode == 0 and not timed_out and not cancelled:
        return {"category": "ok", "recoverable": False, "reason": "clean"}
    if timed_out:
        return {"category": "timeout", "recoverable": True, "reason": "执行超时；可在有界恢复内重试"}
    if cancelled:
        return {"category": "aborted", "recoverable": False, "reason": "任务被中止/租约丢失"}
    if ("error_max_budget_usd" in d or "budget" in d or "402" in d):
        return {"category": "budget_exhausted", "recoverable": False, "reason": "模型费用预算耗尽；需追加预算"}
    if ("api_error_status" in d or "429" in d or "rate" in d or "too many" in d):
        return {"category": "provider_error", "recoverable": True, "reason": "模型提供方限流/暂时不可用；可在有界恢复内重试"}
    if ("requires approval" in d or "permission" in d):
        return {"category": "permission_error", "recoverable": False, "reason": "工具/权限被拒"}
    if ("协议" in d or "无法解析" in d or "not json" in d or "protocol" in d):
        return {"category": "protocol_error", "recoverable": False, "reason": "模型输出不符合研究协议"}
    return {"category": "execution_error", "recoverable": False, "reason": "执行异常"}


def fail_session(conn, session_id, worker_id, *, category, recoverable, reason, max_retries=2):
    """M4 bounded recovery: record a classified failure and either auto-re-queue the
    session (recoverable && under retry cap) for the next worker tick to retry, or
    leave it failed. Increments retry_count on every failure attempt."""
    _lock(conn, session_id)
    row = _row(conn, session_id)
    if row['lease_owner'] != worker_id:
        return False
    retry_count = row['retry_count'] or 0
    if recoverable and retry_count < max_retries:
        status = 'queued'
        retry_count += 1
        note = '将在有界恢复内重试（第 %d/%d 次）' % (retry_count, max_retries)
    else:
        status = 'failed'
        note = ('已达到有界恢复上限' if recoverable else '不可恢复失败') + '；停止自动重试'
    failure = {'category': category, 'recoverable': recoverable, 'reason': reason, 'attempt': retry_count}
    conn.execute(
        'UPDATE research_sessions SET status=?,lease_owner=NULL,lease_expires_at=NULL,latest_error=?,retry_count=?,latest_failure_json=?,updated_at=? WHERE id=?',
        (status, reason, retry_count, _json(failure), utcnow(), session_id),
    )
    append_event(conn, session_id, status, '等待有界恢复重试' if status == 'queued' else '本轮执行失败', note + '：' + reason)
    return True


def session_scope(row_or_dict) -> str:
    """M4 experience feedback: a stable target scope key so later sessions on the
    same material can consume prior research. Without repo/url, falls back to a
    per-session objective scope (less sharing)."""
    r = row_or_dict
    if 'repo' in r.keys() and r['repo']:
        return 'repo:' + str(r['repo'])
    if 'url' in r.keys() and r['url']:
        return 'url:' + str(r['url'])
    return 'objective'


def list_experiences_for_scope(conn, scope, session_id=None, limit=20):
    """M4 experience feedback: prior distilled findings/lessons for the same target
    scope from OTHER sessions, newest-first, each carrying its source session/ref."""
    if not conn.in_transaction:
        conn.execute('BEGIN')
    rows = conn.execute(
        'SELECT * FROM research_experiences WHERE scope_key=? AND source_session_id!=? ORDER BY created_at DESC,id DESC LIMIT ?',
        (scope, session_id or '', limit),
    ).fetchall()
    return [_decode(row) for row in rows]


def distill_experiences(conn, session_id):
    """M4 experience feedback: after a successful/mature session, distill confirmed
    findings (and any final classified failure) into shareable research_experiences
    rows keyed by target scope, so later audits of the same material can learn from
    them. Scoring/idempotency: one row per confirmed finding; at most one lesson per
    session."""
    row = _row(conn, session_id)
    scope = session_scope(row)
    created = []
    for f in conn.execute(
        "SELECT id,title,description FROM research_findings WHERE session_id=? AND status='confirmed'",
        (session_id,),
    ):
        content = (str(f['title']) + '；' + str(f['description'] or '')).strip()[:2000]
        if not content:
            continue
        already = conn.execute(
            "SELECT 1 FROM research_experiences WHERE source_session_id=? AND kind='finding' AND ref=?",
            (session_id, 'finding ' + f['id']),
        ).fetchone()
        if already:
            continue
        eid = _id('exp')
        conn.execute(
            "INSERT INTO research_experiences (id,source_session_id,scope_key,kind,content,ref,created_at) VALUES (?,?,?,?,?,?,?)",
            (eid, session_id, scope, 'finding', content, 'finding ' + f['id'], utcnow()),
        )
        created.append(eid)
    if row['latest_failure_json']:
        lf = json.loads(row['latest_failure_json'])
        if lf and lf.get('reason') and not conn.execute(
            "SELECT 1 FROM research_experiences WHERE source_session_id=? AND kind='lesson'", (session_id,),
        ).fetchone():
            eid = _id('exp')
            conn.execute(
                "INSERT INTO research_experiences (id,source_session_id,scope_key,kind,content,ref,created_at) VALUES (?,?,?,?,?,?,?)",
                (eid, session_id, scope, 'lesson', str(lf['reason'])[:2000],
                 'failure ' + str(lf.get('category') or '') + ' attempt ' + str(lf.get('attempt')), utcnow()),
            )
            created.append(eid)
    return created


def _hash_file_tree(root: Path) -> str:
    """Stable hash of a code tree (relative paths + per-file sha256), ignoring .git."""
    h = hashlib.sha256()
    if not root.exists():
        return h.hexdigest()
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if '.git' in rel.parts:
            continue
        if p.is_file():
            h.update(rel.as_posix().encode())
            try:
                h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
            except OSError:
                continue
    return h.hexdigest()


def fingerprint_repo(path) -> str:
    return _hash_file_tree(Path(path))


def fingerprint_url(url: str, probe: str = '/', timeout: int = 8) -> str:
    """Best-effort fingerprint of a running target: status + body hash. Unreachable
    targets hash as a distinct 'unreachable:<url>' so reachability changes too."""
    base = (url or '').rstrip('/') + '/' + probe.lstrip('/')
    try:
        with urllib.request.urlopen(base, timeout=timeout) as resp:
            body = resp.read()
            return hashlib.sha256(('%d' % resp.status).encode() + body).hexdigest()
    except Exception:
        return 'unreachable:' + hashlib.sha256((url or '').encode()).hexdigest()


def recheck_scope(conn, scope, fingerprint):
    """M4 change trigger: compare the current target fingerprint against the stored
    baseline for this scope. Missing baseline is recorded (first, unchanged); a change
    updates the baseline so repeats are quiet until the next change."""
    stored = conn.execute(
        'SELECT fingerprint FROM research_changewatch WHERE scope_key=?', (scope,),
    ).fetchone()
    now = utcnow()
    if stored is None:
        conn.execute(
            'INSERT INTO research_changewatch (scope_key,fingerprint,checked_at) VALUES (?,?,?) ON CONFLICT(scope_key) DO UPDATE SET fingerprint=excluded.fingerprint, checked_at=excluded.checked_at',
            (scope, fingerprint, now),
        )
        return {'changed': False, 'first': True}
    if stored['fingerprint'] == fingerprint:
        conn.execute('UPDATE research_changewatch SET checked_at=? WHERE scope_key=?', (now, scope))
        return {'changed': False, 'first': False}
    conn.execute(
        'UPDATE research_changewatch SET fingerprint=?,checked_at=?,changed_at=? WHERE scope_key=?',
        (fingerprint, now, now, scope),
    )
    return {'changed': True, 'first': False}


def requeue_for_reaudit(conn, session_id, changed):
    """M4 change trigger: when a completed session's target changed, re-queue it as a
    re-audit round (same authorization/budget/history, new run batch) so the worker
    re-checks whether earlier findings still hold."""
    _lock(conn, session_id)
    row = _row(conn, session_id)
    scope = session_scope(row)
    if row['status'] != 'completed':
        return {'changed': changed, 'requeued': False, 'reason': '仅已完成的研究可作为逐轮复测目标'}
    conn.execute(
        "UPDATE research_sessions SET status='queued',phase=1,next_direction='检测到目标材料变化；复核先前发现是否仍成立，并对新增改动补充检查。',latest_error=NULL,updated_at=? WHERE id=?",
        (utcnow(), session_id),
    )
    conn.execute("UPDATE projects SET status='active' WHERE id=?", (session_id,))
    append_event(conn, session_id, 'change', '检测到目标材料变化', '已重新排队进行一次复测，复核先前发现是否仍成立。', {'scope': scope})
    return {'changed': changed, 'requeued': True, 'scope': scope}


def auto_finalize_if_exhausted(conn, session_id):
    """M1 close-out: after a NON-terminal step lands in `paused`, if the session can no
    longer start another step (cost/seconds/requests all spent, or the max_steps cap is
    reached), auto-close it into `completed` with a report rather than parking forever.
    Honest by construction: it never fabricates findings; the report states that
    unverified items remain unverified. It is a no-op for terminal/waiting/running
    states and never fires while budget remains."""
    _lock(conn, session_id)
    row = _row(conn, session_id)
    if row['status'] != 'paused':
        return {'finalized': False, 'reason': '仅非终态暂停会话可自动收束'}
    usage = json.loads(row['usage_json'])
    budget = json.loads(row['budget_json'])
    remain_cost = max(0.0, float(budget.get('max_cost_usd') or 0) - float(usage.get('cost_usd') or 0))
    remain_sec = max(0, int(budget.get('minutes') or 0) * 60 - int(usage.get('elapsed_seconds') or 0))
    remain_req = max(0, int(budget.get('requests') or 0) - int(usage.get('requests') or 0))
    max_steps = int(budget.get('max_steps') or 0)
    steps_exhausted = max_steps > 0 and int(usage.get('steps') or 0) >= max_steps
    can_continue = (remain_cost > 0 or remain_sec > 0 or remain_req > 0) and not steps_exhausted
    if can_continue:
        return {'finalized': False, 'reason': '仍有可继续的预算或步骤额度'}
    conn.execute(
        "UPDATE research_sessions SET status='completed',phase=3,updated_at=? WHERE id=?",
        (utcnow(), session_id),
    )
    conn.execute("UPDATE projects SET status='completed' WHERE id=?", (session_id,))
    append_event(
        conn, session_id, 'completed', '本轮研究已自动收束',
        '成本/时长/请求或最大步骤数已达到上限，无法再启动新步骤；执行层自动收束。未验证事项保留为未验证，不代表目标安全或检查完整。',
    )
    report = create_report(conn, session_id)
    return {'finalized': True, 'report_id': report['id']}


def recover_session(conn,session_id,worker_id):
    """Only call after proving the owner's process is dead; expiry alone is insufficient."""
    _lock(conn,session_id)
    row=_row(conn,session_id)
    if row['lease_owner']!=worker_id: return False
    return finish_run(conn,session_id,worker_id,'queued','上次执行进程已停止，保留预算与检查点后重新排队')


def reserve_usage(conn,session_id,worker_id,*,requests=0,steps=0,cost_usd=0,elapsed_seconds=0):
    _lock(conn,session_id)
    row=_row(conn,session_id)
    if row['status']!='running' or row['lease_owner']!=worker_id or not row['lease_expires_at'] or row['lease_expires_at']<=utcnow():
        raise HTTPException(409,'研究未运行、暂停中或执行租约已失效')
    usage,budget=json.loads(row['usage_json']),json.loads(row['budget_json'])
    increments={'requests':requests,'steps':steps,'cost_usd':cost_usd,'elapsed_seconds':elapsed_seconds}
    if any(not math.isfinite(v) or v<0 for v in increments.values()): raise ValueError('Usage increments must be finite and nonnegative')
    if not isinstance(requests,int) or not isinstance(steps,int): raise ValueError('Request and step increments must be integers')
    limits={'requests':budget['requests'],'steps':budget['max_steps'],'cost_usd':budget['max_cost_usd'],'elapsed_seconds':budget['minutes']*60}
    updated={k:usage.get(k,0)+v for k,v in increments.items()}
    if any(updated[k]>limit or (usage.get(k,0)>=limit and any(increments.values())) for k,limit in limits.items()):
        raise HTTPException(402,'研究预算已耗尽，停止新增执行')
    conn.execute('UPDATE research_sessions SET usage_json=?,updated_at=? WHERE id=?',(_json(updated),utcnow(),session_id))
    return updated


def account_usage(conn,session_id,worker_id,*,requests=0,steps=0,cost_usd=0,elapsed_seconds=0):
    """Record actual consumed usage for a run that already happened.

    Unlike ``reserve_usage`` this is post-hoc accounting and never raises when the
    approved limits are exceeded: a completed run's real cost/elapsed must be recorded
    so the budget line and the report reflect reality (and later resume is blocked). It
    still requires the worker to own the lease so a lost task cannot keep writing usage.
    """
    _lock(conn,session_id)
    row=_row(conn,session_id)
    if row['lease_owner']!=worker_id: return False
    usage=json.loads(row['usage_json'])
    increments={'requests':requests,'steps':steps,'cost_usd':cost_usd,'elapsed_seconds':elapsed_seconds}
    if any(not math.isfinite(v) or v<0 for v in increments.values()): raise ValueError('Usage increments must be finite and nonnegative')
    if not isinstance(requests,int) or not isinstance(steps,int): raise ValueError('Request and step increments must be integers')
    updated={k:usage.get(k,0)+v for k,v in increments.items()}
    conn.execute('UPDATE research_sessions SET usage_json=?,updated_at=? WHERE id=?',(_json(updated),utcnow(),session_id))
    return updated


def update_checkpoint(conn,session_id,worker_id,*,phase=None,next_direction=None,stack=None,worker_session_id=None,cursor=None):
    _lock(conn,session_id)
    if _row(conn,session_id)['lease_owner']!=worker_id: return False
    values={}
    if phase is not None:
        if phase not in range(4): raise ValueError('Invalid phase')
        values['phase']=phase
    if next_direction is not None: values['next_direction']=next_direction
    if stack is not None: values['stack_json']=_json(stack)
    if worker_session_id is not None: values['worker_session_id']=worker_session_id
    if cursor is not None: values['cursor_json']=_json(cursor)
    values['updated_at']=utcnow()
    conn.execute('UPDATE research_sessions SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),session_id))
    return True


def record_evidence(conn,session_id,kind,title,content,metadata=None,*,worker_id=None):
    _lock(conn,session_id)
    if worker_id is not None and _row(conn,session_id)['lease_owner']!=worker_id:
        return None  # ownership lost; a stale worker must not write results
    eid=_id('evidence')
    conn.execute('INSERT INTO research_evidence (id,session_id,kind,title,content,metadata_json,time) VALUES (?,?,?,?,?,?,?)',(eid,session_id,kind,title,content,_json(metadata or {}),utcnow()))
    return _decode(conn.execute('SELECT * FROM research_evidence WHERE id=?',(eid,)).fetchone())


def record_finding(conn,session_id,title,description,status='pending',evidence_ids=None,impact='',limitations='',*,worker_id=None):
    _lock(conn,session_id)
    if worker_id is not None and _row(conn,session_id)['lease_owner']!=worker_id:
        return None  # ownership lost; a stale worker must not claim results
    evidence_ids=list(dict.fromkeys(evidence_ids or []))
    if status not in ('pending','confirmed','rejected'): raise ValueError('Invalid finding status')
    if status=='confirmed' and not evidence_ids: raise ValueError('Confirmed findings require evidence')
    for eid in evidence_ids:
        if not conn.execute('SELECT 1 FROM research_evidence WHERE session_id=? AND id=?',(session_id,eid)).fetchone(): raise ValueError('Evidence does not belong to this research')
    fid,now=_id('finding'),utcnow()
    conn.execute('INSERT INTO research_findings (id,session_id,title,description,status,evidence_ids_json,impact,limitations,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',(fid,session_id,title,description,status,_json(evidence_ids),impact,limitations,now,now))
    if status=='confirmed':
        fact_id=next_fact_id(conn,session_id)
        conn.execute('INSERT INTO facts (id,project_id,description) VALUES (?,?,?)',(fact_id,session_id,title+'；研究发现 '+fid+'；证据 '+', '.join(evidence_ids)))
    append_event(conn,session_id,'finding',title,description,evidence_ids=evidence_ids,finding_id=fid)
    return _decode(conn.execute('SELECT * FROM research_findings WHERE id=?',(fid,)).fetchone())


def render_report(session):
    lines=['# '+session['title'],'','目标：'+session['objective'],'','状态：'+session['status'],
        '','网站：'+(session['url'] or '未提供'),'代码：'+(session['repo'] or '未提供'),
        '','授权版本：'+str(session['authorization']['revision']),'','## 发现','']
    if not session['findings']: lines.append('当前没有已记录的漏洞发现。这不代表目标安全或检查已经完整。')
    for finding in session['findings']:
        lines += ['### '+finding['title'],'','验证状态：'+finding['status'],finding['description'],'影响：'+(finding['impact'] or '未评估'),'限制：'+(finding['limitations'] or '未单独记录'),'证据：'+(', '.join(finding['evidence_ids']) or '无'),'']
    lines += ['## 证据','']
    for evidence in session['evidence']:
        fence='~'*max(3,1+max((len(match) for match in re.findall(r'~+',evidence['content'])),default=0))
        lines += ['### '+evidence['id']+' · '+evidence['title'],'','类型：'+evidence['kind']+'；采集时间：'+evidence['time'],'',fence+'text',evidence['content'],fence,'','元数据：'+_json(evidence['metadata']),'']
    lines += ['## 研究过程','']
    for event in session['events']: lines.append('- '+event['time']+' · '+event['title']+'：'+event['description'])
    lines += ['','## 尚未验证与执行限制','',session['latest_error'] or '以逐项证据与发现的验证状态为准；未记录的检查不能视为完成。','', '预算：'+_json(session['budget']),'已用：'+_json(session['usage']),'']
    return '\n'.join(lines)


def update_budget(conn,session_id,body):
    _lock(conn,session_id)
    row=_row(conn,session_id)
    if row['status'] in ('running','pause_requested'):
        raise HTTPException(409,'请先暂停并等待执行器停止，再调整预算')
    old=json.loads(row['budget_json'])
    budget={**old,**body.budget.model_dump(exclude_unset=True)}
    if budget==old: return get_session(conn,session_id)
    increased=any(budget[k]>old[k] for k in budget)
    if increased and not body.authorization_confirmed:
        raise HTTPException(403,'增加预算需要确认本次差额')
    usage=json.loads(row['usage_json'])
    if any(usage.get(k,0)>v for k,v in (('elapsed_seconds',budget['minutes']*60),('requests',budget['requests']),('steps',budget['max_steps']),('cost_usd',budget['max_cost_usd']))):
        raise HTTPException(422,'预算不能低于已经消耗的额度')
    rev=row['authorization_revision']+1
    auth=_authorization(row['url'],row['repo'],rev,budget)
    if not increased: auth['confirmed_at']=json.loads(row['authorization_json'])['confirmed_at']
    conn.execute('UPDATE research_sessions SET budget_json=?,authorization_revision=?,authorization_json=? WHERE id=?',(_json(budget),rev,_json(auth),session_id))
    conn.execute('INSERT INTO research_authorizations VALUES (?,?,?,?)',(session_id,rev,_json(auth),utcnow()))
    append_event(conn,session_id,'budget','研究预算已更新','已用额度完整保留；新额度不会自动启动任务。',{'previous':old,'current':budget,'revision':rev})
    return get_session(conn,session_id)


def list_reports(conn, session_id, before=None, limit=20):
    """Read metadata in insertion order; an opaque report ID anchors older pages."""
    if not 1 <= limit <= 100:
        raise ValueError('Report page size must be between 1 and 100')
    if not conn.in_transaction:
        conn.execute('BEGIN')
    _row(conn, session_id)
    params = [session_id]
    condition = ''
    if before is not None:
        cursor = conn.execute(
            'SELECT rowid FROM research_reports WHERE session_id=? AND id=?',
            (session_id, before),
        ).fetchone()
        if cursor is None:
            raise HTTPException(404, '研究报告游标不存在')
        condition = 'AND rowid < ?'
        params.append(cursor[0])
    params.append(limit + 1)
    rows = conn.execute(
        f'SELECT id,session_id,event_seq,created_at,sha256 FROM research_reports '
        f'WHERE session_id=? {condition} ORDER BY rowid DESC LIMIT ?', params,
    ).fetchall()
    items = [dict(row) for row in rows[:limit]]
    has_more = len(rows) > limit
    return {'items': items, 'has_more': has_more,
            'next_cursor': items[-1]['id'] if has_more else None}


def get_report(conn,session_id,report_id):
    row=conn.execute('SELECT * FROM research_reports WHERE session_id=? AND id=?',(session_id,report_id)).fetchone()
    if row is None:
        raise HTTPException(404,'研究报告不存在')
    result=dict(row)
    result['projection']=json.loads(result.pop('projection_json'))
    return result


def create_report(conn,session_id):
    # Take the writer lock before reading so a simultaneous Hint/material update
    # cannot split the projection, event cursor, and Markdown across revisions.
    _lock(conn,session_id)
    session=get_session(conn,session_id)
    markdown=render_report(session)
    projection={
        'title':session['title'], 'objective':session['objective'],
        'status':session['status'], 'findings':session['findings'],
        'evidence':[{k:evidence[k] for k in ('id','kind','title','time')} for evidence in session['evidence']],
        'scope':{'url':session['url'],'repo':session['repo'],'authorization_revision':session['authorization_revision']},
        'limits':{'budget':session['budget'],'usage':session['usage'],'latest_error':session['latest_error']},
        'next':session['next'],
    }
    report_id=_id('report')
    conn.execute('INSERT INTO research_reports (id,session_id,event_seq,created_at,sha256,markdown,projection_json) VALUES (?,?,?,?,?,?,?)',
        (report_id,session_id,session['event_seq'],utcnow(),hashlib.sha256(markdown.encode('utf-8')).hexdigest(),markdown,_json(projection)))
    return get_report(conn,session_id,report_id)
# ---------------------------------------------------------------------------
# Reliability hardening: strict protocol validation, run-batch ledger and the
# single transactional results-commit entry point.
# ---------------------------------------------------------------------------


def validate_payload(payload):
    """Pure-function, whole-package validation of a worker's structured output.

    Enforced before ANY write: ``terminal`` and ``awaiting_input`` must be REAL bools
    (a JSON string ``"false"``, an int, or a non-bool object is a protocol error, not
    implicit falsy); they are mutually exclusive (both true = conflict = protocol
    error), giving a strict three-state protocol — completed / awaiting input /
    not-completed. evidence/findings must be lists of dicts with a recognizable
    structure. A terminal run with no findings must still carry a legitimate textual
    result explanation. Malformed output NEVER yields a completion report.

    Returns (valid, reason, normalized) and never mutates or writes anything.
    """
    if not isinstance(payload, dict):
        return False, '输出必须是 JSON 对象', None

    terminal = payload.get('terminal')
    awaiting = payload.get('awaiting_input')
    if not isinstance(terminal, bool):
        return False, f'terminal 必须是布尔值（得到 {type(terminal).__name__}）', None
    if not isinstance(awaiting, bool):
        return False, f'awaiting_input 必须是布尔值（得到 {type(awaiting).__name__}）', None
    if terminal and awaiting:
        return False, 'terminal 与 awaiting_input 冲突（不能同时为 true）', None

    evidence = payload.get('evidence')
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        return False, 'evidence 必须是数组', None
    normalized_evidence = []
    for idx, item in enumerate(evidence):
        if not isinstance(item, dict):
            return False, f'evidence[{idx}] 必须是对象', None
        kind = item.get('kind') if isinstance(item.get('kind'), str) else 'note'
        title = item.get('title')
        content = item.get('content')
        if not isinstance(title, str) or not title.strip():
            return False, f'evidence[{idx}] 缺少非空 title', None
        if not isinstance(content, str):
            return False, f'evidence[{idx}].content 必须是字符串', None
        metadata = item.get('metadata')
        if metadata is not None and not isinstance(metadata, dict):
            return False, f'evidence[{idx}].metadata 必须是对象', None
        normalized_evidence.append({
            'kind': kind[:32],
            'title': title[:240],
            'content': content,
            'metadata': metadata if isinstance(metadata, dict) else {},
        })

    findings = payload.get('findings')
    if findings is None:
        findings = []
    if not isinstance(findings, list):
        return False, 'findings 必须是数组', None
    normalized_findings = []
    for idx, item in enumerate(findings):
        if not isinstance(item, dict):
            return False, f'findings[{idx}] 必须是对象', None
        title = item.get('title')
        description = item.get('description')
        if not isinstance(title, str) or not title.strip():
            return False, f'findings[{idx}] 缺少非空 title', None
        if not isinstance(description, str):
            return False, f'findings[{idx}].description 必须是字符串', None
        status = item.get('status') if isinstance(item.get('status'), str) else 'pending'
        if status not in ('pending', 'confirmed', 'rejected'):
            return False, f'findings[{idx}].status 非法：{status}', None
        for key in ('impact', 'limitations'):
            val = item.get(key)
            if val is not None and not isinstance(val, str):
                return False, f'findings[{idx}].{key} 必须是字符串', None
        normalized_findings.append({
            'title': title[:240],
            'description': description,
            'status': status,
            'impact': (item.get('impact') or '') if isinstance(item.get('impact') or '', str) else '',
            'limitations': (item.get('limitations') or '') if isinstance(item.get('limitations') or '', str) else '',
        })

    phase = payload.get('phase')
    if phase is not None and (not isinstance(phase, int) or isinstance(phase, bool) or phase not in range(4)):
        return False, 'phase 必须是 0..3 的整数', None

    summary = payload.get('summary')
    if summary is not None and not isinstance(summary, str):
        return False, 'summary 必须是字符串', None
    next_direction = payload.get('next_direction')
    if next_direction is not None and not isinstance(next_direction, str):
        return False, 'next_direction 必须是字符串', None

    # A terminal completion with zero findings must still explain the (non)result.
    if terminal and not normalized_findings:
        explanation = (summary or '').strip()
        if not explanation:
            return False, '无发现的完成必须有合法的结果说明（summary 不能为空）', None

    normalized = {
        'summary': (summary or '') if isinstance(summary, str) else '',
        'evidence': normalized_evidence,
        'findings': normalized_findings,
        'next_direction': next_direction if isinstance(next_direction, str) else '',
        'phase': phase,
        'terminal': terminal,
        'awaiting_input': awaiting,
    }
    return True, None, normalized


def _run_account(conn, session_id, run_id):
    row = conn.execute(
        'SELECT * FROM research_run_accounts WHERE run_id=? AND session_id=?',
        (run_id, session_id),
    ).fetchone()
    return dict(row) if row is not None else None


def create_run_account(conn, session_id, worker_id):
    """Open a fresh independent run batch for a claim/start. Each claim gets a new
    run_id so batch attribution, settlement and dedup are per-run; the session's
    current_run_id tracks the active batch and a stale cost hold is cleared."""
    _lock(conn, session_id)
    run_id = _id('run')
    conn.execute(
        'INSERT INTO research_run_accounts (run_id,session_id,worker_id,created_at,cost_status) VALUES (?,?,?,?,?)',
        (run_id, session_id, worker_id, utcnow(), 'pending_check'),
    )
    conn.execute(
        'UPDATE research_sessions SET current_run_id=?, cost_pending_check=0, updated_at=? WHERE id=?',
        (run_id, utcnow(), session_id),
    )
    return run_id


def settle_run(conn, session_id, run_id, worker_id, *, requests=0, elapsed_seconds=0,
               final_cost=None, expect_final_cost=False):
    """Finalize ONE run batch against the session budget AND its ledger row.

    Idempotent: a settled batch is never double-charged; a repeated settlement returns
    the already-recorded outcome without re-applying elapsed/cost. It does NOT require
    lease ownership — a worker that lost the lease may still file this batch's real
    consumption (elapsed/cost) for a truthful budget, but that path can never write
    research results (that is commit_run_results' owner-guarded single entry).

    - elapsed_seconds/requests are applied to cumulative usage once.
    - A numeric final_cost is added to cumulative and the batch marked 'recorded';
      the released budget is therefore truthfully reduced.
    - If the run consumed provider resources but no final cost surfaced
      (expect_final_cost), the batch and SESSION are marked cost_pending_check: the
      unknown cost is not released and resume is blocked until it is credited.
    """
    _lock(conn, session_id)
    row = _row(conn, session_id)
    run = _run_account(conn, session_id, run_id)
    if run is None:
        raise HTTPException(404, '执行批次不存在')
    if run['worker_id'] != worker_id:
        raise HTTPException(409, '执行批次不属于该执行者')
    usage = json.loads(row['usage_json'])
    updated = dict(usage)
    if run['settled']:
        return {'run_id': run_id, 'settled': 1, 'cost_status': run['cost_status']}
    if elapsed_seconds:
        updated['elapsed_seconds'] = usage.get('elapsed_seconds', 0) + elapsed_seconds
    if requests:
        updated['requests'] = usage.get('requests', 0) + requests

    cost_status = 'recorded'
    if final_cost is not None:
        if (not isinstance(final_cost, (int, float)) or isinstance(final_cost, bool)
                or not math.isfinite(float(final_cost)) or final_cost < 0):
            raise ValueError('Final cost must be a finite nonnegative number')
        updated['cost_usd'] = usage.get('cost_usd', 0) + float(final_cost)
        conn.execute(
            "UPDATE research_sessions SET usage_json=?, cost_pending_check=0, updated_at=? WHERE id=?",
            (_json(updated), utcnow(), session_id),
        )
    elif expect_final_cost:
        cost_status = 'pending_check'
        conn.execute(
            "UPDATE research_sessions SET usage_json=?, cost_pending_check=1, updated_at=? WHERE id=?",
            (_json(updated), utcnow(), session_id),
        )
    else:
        conn.execute(
            "UPDATE research_sessions SET usage_json=?, updated_at=? WHERE id=?",
            (_json(updated), utcnow(), session_id),
        )
    conn.execute(
        "UPDATE research_run_accounts SET settled=1,finished_at=?,cost_status=?,requests=?,elapsed_seconds=?,cost_usd=? WHERE run_id=?",
        (utcnow(), cost_status, requests, elapsed_seconds,
         float(final_cost) if final_cost is not None else 0.0, run_id),
    )
    _maybe_budget_warn(conn, session_id, usage, updated, json.loads(row['budget_json']))
    return {'run_id': run_id, 'settled': 1, 'cost_status': cost_status}


def _maybe_budget_warn(conn, session_id, usage, updated, budget):
    """M4 cost-governance telemetry: emit a single budget_warn event the first time
    remaining cost budget drops below each threshold after a settlement, so the UI
    surfaces a shrinking runway instead of failing silently at the hard gate."""
    try:
        max_cost = float(budget.get('max_cost_usd') or 0)
    except (TypeError, ValueError):
        return
    if max_cost <= 0:
        return
    before = max(0.0, max_cost - float(usage.get('cost_usd') or 0))
    after = max(0.0, max_cost - float(updated.get('cost_usd') or 0))
    pb, pa = before / max_cost, after / max_cost
    for threshold in (0.50, 0.25, 0.10):
        if pa <= threshold < pb:
            append_event(
                conn, session_id, 'budget_warn',
                '研究预算剩余不足 %d%%' % int(threshold * 100),
                '剩余成本预算已跨越预警阈值；到达硬门时将停止新增执行且可追加预算后恢复。',
                {'remaining': {
                    'cost_usd': round(after, 4),
                    'cost_pct': round(pa * 100, 1),
                    'requests_remaining': max(0, int(budget.get('requests') or 0) - int(updated.get('requests') or 0)),
                    'seconds_remaining': max(0, int(budget.get('minutes') or 0) * 60 - int(updated.get('elapsed_seconds') or 0)),
                }},
            )
            break


def resolve_pending_cost(conn, session_id, final_cost):
    """Confirm/credit an outstanding cost on a cost_pending_check session and clear
    the hold so resume may proceed (resume continues to use cumulative usage)."""
    _lock(conn, session_id)
    row = _row(conn, session_id)
    if (not isinstance(final_cost, (int, float)) or isinstance(final_cost, bool)
            or not math.isfinite(float(final_cost)) or final_cost < 0):
        raise ValueError('Final cost must be a finite nonnegative number')
    run = None
    if row['current_run_id']:
        run = _run_account(conn, session_id, row['current_run_id'])
    usage = json.loads(row['usage_json'])
    updated = dict(usage)
    updated['cost_usd'] = usage.get('cost_usd', 0) + float(final_cost)
    conn.execute(
        "UPDATE research_sessions SET usage_json=?, cost_pending_check=0, updated_at=? WHERE id=?",
        (_json(updated), utcnow(), session_id),
    )
    if run is not None:
        conn.execute(
            "UPDATE research_run_accounts SET cost_status='recorded', cost_usd=cost_usd+? WHERE run_id=?",
            (float(final_cost), run['run_id']),
        )


def commit_run_results(conn, session_id, run_id, worker_id, payload):
    """THE single transactional entry for committing a run batch's research results.

    Under one SQLite writer transaction it: takes the writer lock (so the lease cannot
    be reassigned mid-commit), validates run_id / owner / lease validity / session
    state, then writes evidence, findings, events, checkpoint and terminal state AND
    an auto report (on completion) all-or-nothing. Empty results are a valid commit.
    A pending pause request preempts a late completion. Reports are bound to a
    successful commit and deduplicated per run batch (``committed`` guard). Human / API
    write paths are separate and untouched. Returns a decision dict; it raises only on
    hard failures (the caller rolls back the whole transaction).
    """
    _lock(conn, session_id)
    row = _row(conn, session_id)

    if row['lease_owner'] != worker_id:
        return {'ok': False, 'reason': 'ownership_lost', 'detail': 'run 结果不属于当前执行者'}
    if not row['lease_expires_at'] or row['lease_expires_at'] <= utcnow():
        return {'ok': False, 'reason': 'ownership_lost', 'detail': '执行租约已失效'}

    valid, reason, normalized = validate_payload(payload)
    if not valid:
        return {'ok': False, 'reason': 'protocol_error', 'detail': reason}

    if row['status'] == 'pause_requested':
        return {'ok': False, 'reason': 'paused', 'detail': '用户已请求暂停，晚到的结果不覆盖'}

    run = _run_account(conn, session_id, run_id or row['current_run_id'])
    if run is None or run['worker_id'] != worker_id:
        return {'ok': False, 'reason': 'ownership_lost', 'detail': '执行批次不属于当前执行者'}
    if run['committed']:
        return {'ok': True, 'already_committed': True, 'terminal': False,
                'status': None, 'report_id': None}

    evidence_ids = []
    terminal = normalized['terminal']
    awaiting = normalized['awaiting_input']

    for item in normalized.get('evidence') or []:
        record = record_evidence(
            conn, session_id, item['kind'], item['title'], item['content'],
            metadata=item.get('metadata'), worker_id=worker_id,
        )
        if record is None:
            raise HTTPException(409, '结果写入时租约已转移')
        evidence_ids.append(record['id'])
        append_event(conn, session_id, 'evidence', item['title'], item['content'][:800],
                     evidence_ids=[record['id']])

    for item in normalized.get('findings') or []:
        record_finding(
            conn, session_id, item['title'], item['description'],
            status=item['status'], evidence_ids=evidence_ids,
            impact=item.get('impact') or '', limitations=item.get('limitations') or '',
            worker_id=worker_id,
        )

    phase = normalized.get('phase')
    if awaiting or (normalized.get('summary') or '').strip():
        next_direction = (normalized.get('next_direction') or '')[:2000] or None
        update_checkpoint(
            conn, session_id, worker_id,
            phase=phase if isinstance(phase, int) and phase in range(4) else None,
            next_direction=next_direction,
        )

    if terminal:
        conn.execute(
            "UPDATE research_sessions SET status='completed',lease_owner=NULL,lease_expires_at=NULL,phase=3,updated_at=? WHERE id=?",
            (utcnow(), session_id),
        )
        conn.execute("UPDATE projects SET status='completed' WHERE id=?", (session_id,))
        append_event(conn, session_id, 'completed', '本轮研究已完成',
                     (normalized.get('summary') or '执行器已保存检查点。')[:2000])
        distill_experiences(conn, session_id)  # M4: learning fed back for later audits
        report = create_report(conn, session_id)
        out_status = 'completed'
        out_report = report['id']
    elif awaiting:
        conn.execute(
            "UPDATE research_sessions SET status='waiting_input',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?",
            (utcnow(), session_id),
        )
        append_event(conn, session_id, 'waiting_input', '研究需要补充条件',
                     (normalized.get('summary') or '请补充材料或调整边界。')[:2000])
        out_status = 'waiting_input'
        out_report = None
    else:
        conn.execute(
            "UPDATE research_sessions SET status='paused',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?",
            (utcnow(), session_id),
        )
        append_event(conn, session_id, 'paused', '本轮研究已暂停', '执行器已保存检查点并停止；可继续探索。')
        out_status = 'paused'
        out_report = None

    conn.execute(
        "UPDATE research_run_accounts SET committed=1, finished_at=COALESCE(finished_at,?) WHERE run_id=?",
        (utcnow(), run['run_id']),
    )
    return {'ok': True, 'reason': None, 'terminal': terminal, 'status': out_status,
            'report_id': out_report}