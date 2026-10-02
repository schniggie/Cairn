from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from cairn.server import db, research_services as service
from cairn.server.routers.research import router
from cairn.server.routers.projects import router as projects_router


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'_db_path',None)
    db.configure(tmp_path/'research.db')
    app=FastAPI()
    app.include_router(router)
    app.include_router(projects_router)
    with TestClient(app) as client:
        yield client


def create(client,**kwargs):
    body={'title':'Access control research','objective':'Review authorized target','url':'http://127.0.0.1:4100','authorization_confirmed':True}
    body.update(kwargs)
    response=client.post('/api/research/sessions',json=body)
    assert response.status_code==201,response.text
    return response.json()


def action(client,session,name,**kwargs):
    return client.post('/api/research/sessions/'+session['id']+'/'+name,**kwargs)


def test_persistence_seeds_blackboard_and_mode_isolation(client,tmp_path):
    session=create(client,repo=str(tmp_path))
    assert session['mode']=='combined' and session['status']=='queued'
    assert {x['kind'] for x in session['assets']}=={'url','repo'}
    assert session['events'][0]['seq']==1
    assert session['authorization']['revision']==1
    assert session['findings']==[] and session['evidence']==[] and session['stack']==[]
    assert client.get('/api/research/sessions/'+session['id']).json()==session
    assert client.get('/api/research/sessions').json()['items'][0]['id']==session['id']
    assert client.get('/projects').json()==[]
    with db.get_conn() as conn:
        assert conn.execute('SELECT project_kind FROM projects WHERE id=?',(session['id'],)).fetchone()[0]=='research'
        assert {row[0] for row in conn.execute('SELECT id FROM facts WHERE project_id=?',(session['id'],))}=={'origin','goal'}
        assert conn.execute('SELECT count(*) FROM intents WHERE project_id=?',(session['id'],)).fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM research_authorizations').fetchone()[0]==1
    old=db._db_path
    db.configure(old)
    assert client.get('/api/research/sessions/'+session['id']).json()['id']==session['id']


@pytest.mark.parametrize('patch,code',[
    ({'authorization_confirmed':False},403),
    ({'url':None},422),
    ({'url':'https://user:secret@example.com'},422),
    ({'url':'file:///etc/passwd'},422),
    ({'url':'http://bad host.test'},422),
    ({'url':'http://example.com:bad'},422),
    ({'url':'http://*.example.com'},422),
    ({'repo':'relative/path'},422),
    ({'repo':'/nonexistent/cairn/repo'},422),
    ({'title':' '},422),
    ({'budget':{'max_cost_usd':-1}},422),
    ({'budget':{'max_steps':0}},422),
    ({'findings':[{'status':'confirmed'}]},422),
])
def test_invalid_creation(client,patch,code):
    body={'title':'t','objective':'o','url':'http://127.0.0.1','authorization_confirmed':True}
    body.update(patch)
    assert client.post('/api/research/sessions',json=body).status_code==code
    assert client.get('/api/research/sessions').json()=={'items':[]}


def test_csrf(client):
    body={'title':'t','objective':'o','url':'localhost','authorization_confirmed':True}
    for headers in ({'Origin':'https://evil.example'},{'Origin':'null'},{'Sec-Fetch-Site':'cross-site'},{'Referer':'http://evil.example/'}):
        assert client.post('/api/research/sessions',json=body,headers=headers).status_code==403
    assert client.post('/api/research/sessions',json=body,headers={'Origin':'http://testserver','Sec-Fetch-Site':'same-origin'}).status_code==201


def test_pause_resume_hint_event_cursor_and_complete(client):
    session=create(client)
    first=action(client,session,'pause').json()
    assert first['status']=='paused'
    assert action(client,session,'pause').json()['event_seq']==first['event_seq']
    second=action(client,session,'resume').json()
    assert second['status']=='queued' and second['authorization']==session['authorization']
    assert second['usage']==session['usage']
    assert action(client,session,'resume').json()['event_seq']==second['event_seq']
    updated=action(client,session,'hints',json={'content':'Review tenant boundary'}).json()
    assert updated['next']=='Review tenant boundary'
    with db.get_conn() as conn:
        assert conn.execute('SELECT content FROM hints WHERE project_id=?',(session['id'],)).fetchone()[0]=='Review tenant boundary'
    events=client.get('/api/research/sessions/'+session['id']+'/events?after=2&limit=2').json()
    assert [e['seq'] for e in events['items']]==[3,4] and events['next_cursor']==4
    assert client.get('/api/research/sessions/'+session['id']+'/events?after=999').json()=={'items':[],'next_cursor':999}
    done=action(client,session,'complete').json()
    assert done['status']=='completed'
    assert action(client,session,'complete').json()['event_seq']==done['event_seq']
    resumed=action(client,session,'resume').json()
    assert resumed['status']=='queued' and resumed['authorization']==session['authorization']
    with db.get_conn() as conn:
        assert conn.execute('SELECT status FROM projects WHERE id=?',(session['id'],)).fetchone()[0]=='active'


def test_material_scope_revision_and_preserve_history(client,tmp_path):
    session=create(client)
    url='/api/research/sessions/'+session['id']+'/materials'
    assert client.patch(url,json={'url':session['url']}).json()['authorization']['revision']==1
    assert client.patch(url,json={'repo':str(tmp_path)}).status_code==403
    detail=client.patch(url,json={'repo':str(tmp_path),'authorization_confirmed':True}).json()
    assert detail['mode']=='combined' and detail['authorization']['revision']==2
    reduced=client.patch(url,json={'url':None}).json()
    assert reduced['mode']=='code' and reduced['authorization']['revision']==3
    assert len(reduced['assets'])==2
    assert client.patch(url,json={'repo':None}).status_code==422
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM research_authorizations').fetchone()[0]==3


def test_running_pause_and_budget_never_reset(client):
    session=create(client,budget={'requests':2,'max_steps':2})
    with db.get_conn() as conn:
        claimed=service.claim_session(conn,'worker')
        assert claimed['id']==session['id']
        service.reserve_usage(conn,session['id'],'worker',steps=1,requests=1)
        assert service.update_checkpoint(conn,session['id'],'worker',phase=1,worker_session_id='claude-session',cursor={'step':1})
    assert action(client,session,'complete').status_code==409
    assert client.patch('/api/research/sessions/'+session['id']+'/materials',json={'url':'http://localhost:2','authorization_confirmed':True}).status_code==409
    assert action(client,session,'pause').json()['status']=='pause_requested'
    assert action(client,session,'resume').status_code==409
    with db.get_conn() as conn:
        with pytest.raises(HTTPException) as exc: service.reserve_usage(conn,session['id'],'worker',steps=1)
        assert exc.value.status_code==409
        assert not service.finish_run(conn,session['id'],'other','completed')
        assert service.finish_run(conn,session['id'],'worker','completed')
    resumed=action(client,session,'resume').json()
    assert resumed['status']=='queued' and resumed['usage']['steps']==1
    assert resumed['worker_session_id']=='claude-session'
    with db.get_conn() as conn:
        service.claim_session(conn,'worker2')
        service.reserve_usage(conn,session['id'],'worker2',steps=1,requests=1)
        with pytest.raises(HTTPException) as exc: service.reserve_usage(conn,session['id'],'worker2',steps=1)
        assert exc.value.status_code==402
        service.finish_run(conn,session['id'],'worker2','waiting_input','预算已耗尽')
    assert action(client,session,'resume').status_code==409
    assert client.get('/api/research/sessions/'+session['id']).json()['usage']['steps']==2


def test_exclusive_claim_and_expired_lease_requires_verified_recovery(client):
    session=create(client)
    def claim(worker):
        with db.get_conn() as conn: return service.claim_session(conn,worker)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim,['worker-a','worker-b']))
    assert sum(r is not None for r in results)==1
    owner=next(r for r in results if r)['lease_owner']
    with db.get_conn() as conn:
        conn.execute("UPDATE research_sessions SET lease_expires_at='2000-01-01T00:00:00Z'")
        with pytest.raises(HTTPException): service.reserve_usage(conn,session['id'],owner,steps=1)
        assert service.claim_session(conn,'new') is None
        assert not service.recover_session(conn,session['id'],'wrong-owner')
        assert service.recover_session(conn,session['id'],owner)
        assert service.claim_session(conn,'new')['id']==session['id']


def test_evidence_linkage_facts_and_truthful_report(client):
    session=create(client)
    other=create(client,title='Other')
    with db.get_conn() as conn:
        evidence=service.record_evidence(conn,session['id'],'http','Authorization response','HTTP/1.1 403 Forbidden',{'status_code':403})
        with pytest.raises(ValueError): service.record_finding(conn,session['id'],'Claim','No proof',status='confirmed')
        with pytest.raises(ValueError): service.record_finding(conn,other['id'],'Claim','Wrong scope',status='confirmed',evidence_ids=[evidence['id']])
        finding=service.record_finding(conn,session['id'],'Access denied','Boundary enforced for this request',status='rejected',evidence_ids=[evidence['id']],limitations='One request only')
        assert conn.execute('SELECT count(*) FROM facts WHERE project_id=?',(session['id'],)).fetchone()[0]==2
    response=client.get('/api/research/sessions/'+session['id']+'/report.md')
    assert response.status_code==200
    assert evidence['id'] in response.text and '403 Forbidden' in response.text and 'One request only' in response.text
    assert finding['id'] not in client.get('/api/research/sessions/'+other['id']+'/report.md').text
    assert '不代表目标安全' in client.get('/api/research/sessions/'+other['id']+'/report.md').text
    assert client.post('/api/research/sessions/'+session['id']+'/findings',json={'status':'confirmed'}).status_code==404


def test_not_found(client):
    assert client.get('/api/research/sessions/missing').status_code==404
    assert client.post('/api/research/sessions/missing/pause').status_code==404


def test_budget_expansion_and_exhausted_resume(client):
    session=create(client,budget={'max_steps':1})
    with db.get_conn() as conn:
        service.claim_session(conn,'worker')
        service.reserve_usage(conn,session['id'],'worker',steps=1)
        service.finish_run(conn,session['id'],'worker','waiting_input','step budget exhausted')
    url='/api/research/sessions/'+session['id']+'/budget'
    assert action(client,session,'resume').status_code==409
    assert client.patch(url,json={'budget':{'max_steps':2}}).status_code==403
    updated=client.patch(url,json={'budget':{'max_steps':2},'authorization_confirmed':True}).json()
    assert updated['usage']['steps']==1 and updated['budget']['max_steps']==2
    assert updated['budget']['requests']==300 and updated['authorization']['revision']==2
    assert updated['status']=='waiting_input'
    assert client.patch(url,json={'budget':{'max_steps':2}}).json()['authorization']['revision']==2
    assert action(client,session,'resume').json()['status']=='queued'
    with db.get_conn() as conn:
        service.claim_session(conn,'worker2')
    assert client.patch(url,json={'budget':{'max_steps':3},'authorization_confirmed':True}).status_code==409


def test_nonfinite_and_negative_usage_rejected(client):
    session=create(client)
    with db.get_conn() as conn:
        service.claim_session(conn,'worker')
        for kwargs in ({'cost_usd':float('nan')},{'elapsed_seconds':float('inf')},{'requests':-1},{'steps':0.5}):
            with pytest.raises(ValueError): service.reserve_usage(conn,session['id'],'worker',**kwargs)
        assert service.get_session(conn,session['id'])['usage']['steps']==0
    url='/api/research/sessions/'+session['id']+'/budget'
    for body in ({'budget':{}},{'budget':{'requests':None}},{'budget':{'minutes':0}}):
        assert client.patch(url,json=body).status_code==422


def test_report_evidence_cannot_escape_fence(client):
    session=create(client)
    with db.get_conn() as conn:
        service.record_evidence(conn,session['id'],'source','Code','a\n~~~\n<script>alert(1)</script>')
    report=client.get('/api/research/sessions/'+session['id']+'/report.md').text
    assert report.startswith('# Access control research\n\n')
    assert 'a\n~~~\n<script>alert(1)</script>' in report
    assert report.count('~~~~')==2


def test_immutable_report_scope_budget_and_hint_changes(client,tmp_path):
    session=create(client)
    base='/api/research/sessions/'+session['id']
    with db.get_conn() as conn:
        conn.execute("UPDATE research_sessions SET worker_session_id='private-session',cursor_json=? WHERE id=?", ('{"secret":"PRIVATE_CURSOR_VALUE"}',session['id']))
        evidence=service.record_evidence(conn,session['id'],'source','Boundary check','a\n~~~~~\n原始证据\r\n',{'internal_note':'PRIVATE_METADATA_VALUE'})
    response=client.post(base+'/reports')
    assert response.status_code==201,response.text
    saved=response.json()
    assert saved['session_id']==session['id'] and saved['event_seq']==session['event_seq']
    assert saved['projection']['scope']['authorization_revision']==1
    assert saved['projection']['evidence']==[{k:evidence[k] for k in ('id','kind','title','time')}]
    assert 'PRIVATE_CURSOR_VALUE' not in response.text
    assert 'private-session' not in response.text
    assert saved['sha256']==hashlib.sha256(saved['markdown'].encode('utf-8')).hexdigest()
    assert evidence['content'] in saved['markdown']
    detail_url=base+'/reports/'+saved['id']
    download=client.get(detail_url+'/report.md')
    assert download.content==saved['markdown'].encode('utf-8')
    assert download.headers['etag']=='"'+saved['sha256']+'"'
    assert client.post(base+'/hints',json={'content':'New direction'}).status_code==200
    assert client.patch(base+'/materials',json={'repo':str(tmp_path),'authorization_confirmed':True}).status_code==200
    assert client.patch(base+'/budget',json={'budget':{'minutes':60},'authorization_confirmed':True}).status_code==200
    current=client.get(base).json()
    assert current['event_seq']>saved['event_seq'] and current['authorization_revision']==3
    assert client.get(detail_url).json()==saved
    assert client.get(detail_url+'/report.md').content==download.content
    latest=client.post(base+'/reports').json()
    assert latest['id']!=saved['id'] and latest['sha256']!=saved['sha256']
    assert latest['projection']['next']=='New direction'
    assert latest['projection']['scope']['repo']==str(tmp_path)
    assert latest['projection']['limits']['budget']['minutes']==60
    with db.get_conn() as conn:
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            conn.execute("UPDATE research_reports SET markdown='changed' WHERE id=?",(saved['id'],))


def test_report_id_scoped_to_research_and_csrf(client):
    first=create(client)
    second=create(client)
    saved=client.post('/api/research/sessions/'+first['id']+'/reports').json()
    wrong='/api/research/sessions/'+second['id']+'/reports/'+saved['id']
    assert client.get(wrong).status_code==404
    assert client.get(wrong+'/report.md').status_code==404
    assert client.post('/api/research/sessions/missing/reports').status_code==404
    assert client.post('/api/research/sessions/'+first['id']+'/reports',headers={'Origin':'http://other.test'}).status_code==403


def test_detail_read_transaction_prevents_mixed_event_snapshot(client):
    session=create(client)
    injected=False
    with db.get_conn() as reader:
        assert not reader.in_transaction
        def during_select(statement):
            nonlocal injected
            if 'SELECT * FROM research_events WHERE' in statement and not injected:
                injected=True
                with db.get_conn() as writer:
                    service.add_hint(writer,session['id'],'Concurrent hint')
        reader.set_trace_callback(during_select)
        detail=service.get_session(reader,session['id'])
        assert reader.in_transaction
        assert injected
        assert detail['event_seq']==session['event_seq']
        assert detail['events']==session['events']
        assert detail['next']==session['next']
    latest=client.get('/api/research/sessions/'+session['id']).json()
    assert latest['event_seq']==session['event_seq']+1
    assert latest['events'][-1]['description']=='Concurrent hint'


def test_report_snapshot_inherits_write_transaction_and_rolls_back(client):
    session=create(client)
    report_id=None
    with pytest.raises(RuntimeError,match='rollback'):
        with db.get_conn() as conn:
            service.add_hint(conn,session['id'],'Uncommitted hint')
            assert conn.in_transaction
            snapshot=service.create_report(conn,session['id'])
            report_id=snapshot['id']
            assert snapshot['event_seq']==session['event_seq']+1
            assert snapshot['projection']['next']=='Uncommitted hint'
            assert 'Uncommitted hint' in snapshot['markdown']
            raise RuntimeError('rollback')
    assert client.get('/api/research/sessions/'+session['id']+'/reports/'+report_id).status_code==404
    assert client.get('/api/research/sessions/'+session['id']).json()['event_seq']==session['event_seq']


def test_report_history_is_readonly_metadata_and_scoped(client, monkeypatch):
    session=create(client)
    other=create(client,title='Other history')
    base='/api/research/sessions/'+session['id']+'/reports'
    assert client.get(base).json()=={'items':[],'next_cursor':None,'has_more':False}
    # Force identical timestamps; ordering must follow insertion, not random UUID.
    monkeypatch.setattr(service,'utcnow',lambda:'2026-09-09T01:02:03Z')
    with db.get_conn() as conn:
        conn.execute("UPDATE research_sessions SET cursor_json=? WHERE id=?",
                     ('{"secret":"PRIVATE_HISTORY_CANARY"}',session['id']))
        saved=[service.create_report(conn,session['id']) for _ in range(5)]
        foreign=service.create_report(conn,other['id'])
    first=client.get(base+'?limit=2').json()
    assert [r['id'] for r in first['items']]==[r['id'] for r in saved[-1:-3:-1]]
    assert first['has_more'] and first['next_cursor']==saved[-2]['id']
    assert all(set(r)=={'id','session_id','created_at','event_seq','sha256'} for r in first['items'])
    assert 'PRIVATE_HISTORY_CANARY' not in str(first)
    # New insertions do not shift the cursor's older pages.
    new=client.post(base).json()
    second=client.get(base,params={'limit':2,'before':first['next_cursor']}).json()
    third=client.get(base,params={'limit':2,'before':second['next_cursor']}).json()
    assert [r['id'] for r in first['items']+second['items']+third['items']]==[r['id'] for r in reversed(saved)]
    assert third['next_cursor'] is None and not third['has_more']
    assert client.get(base+'?limit=1').json()['items'][0]['id']==new['id']
    assert client.get(base,params={'before':saved[0]['id']}).json()=={'items':[],'next_cursor':None,'has_more':False}
    for cursor in (foreign['id'],'unknown',''):
        assert client.get(base,params={'before':cursor}).status_code==404
    assert client.get('/api/research/sessions/missing/reports').status_code==404
    for limit in (0,101,-1,'text'):
        assert client.get(base,params={'limit':limit}).status_code==422
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM research_reports WHERE session_id=?',(session['id'],)).fetchone()[0]==6
        assert service.get_session(conn,session['id'])['event_seq']==session['event_seq']
        for old in saved:
            assert service.get_report(conn,session['id'],old['id'])==old


def test_report_history_read_snapshot_and_no_implicit_commit(client):
    session=create(client)
    with db.get_conn() as conn:
        first=service.create_report(conn,session['id'])
    injected=False
    with db.get_conn() as reader:
        def during_read(statement):
            nonlocal injected
            if 'SELECT id,session_id,event_seq,created_at,sha256 FROM research_reports' in statement and not injected:
                injected=True
                with db.get_conn() as writer:
                    service.create_report(writer,session['id'])
        reader.set_trace_callback(during_read)
        result=service.list_reports(reader,session['id'])
        assert injected and reader.in_transaction
        assert [r['id'] for r in result['items']]==[first['id']]
    with db.get_conn() as conn:
        assert len(service.list_reports(conn,session['id'])['items'])==2
    with pytest.raises(RuntimeError,match='rollback'):
        with db.get_conn() as conn:
            saved=service.create_report(conn,session['id'])
            assert service.list_reports(conn,session['id'])['items'][0]['id']==saved['id']
            raise RuntimeError('rollback')
    assert client.get('/api/research/sessions/'+session['id']+'/reports/'+saved['id']).status_code==404
