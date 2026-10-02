from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.server import db, research_sources as sources
from cairn.server.routers.research import router as research_router
from cairn.server.routers.research_sources import router


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'_db_path',None)
    db.configure(tmp_path/'research.db')
    app=FastAPI()
    app.include_router(research_router)
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def session(client,repo=None):
    body={'title':'Fixture source','objective':'Preserve local materials','url':'http://example.invalid','authorization_confirmed':True}
    if repo: body['repo']=str(repo)
    response=client.post('/api/research/sessions',json=body)
    assert response.status_code==201,response.text
    return response.json()['id']


def endpoint(sid):
    return '/api/research/sessions/'+sid+'/sources'


def capture(client,sid):
    response=client.post(endpoint(sid))
    assert response.status_code==201,response.text
    return response.json()


def make_repo(tmp_path):
    repo=tmp_path/'repo'
    repo.mkdir()
    (repo/'app.py').write_bytes('first\r\nsecond\n第三行\n'.encode())
    (repo/'Dockerfile').write_text('FROM example-not-executed\n')
    (repo/'unknown.custom').write_text('unrecognized text language\n')
    return repo


def test_snapshot_digest_languages_and_immutable_bytes(client,tmp_path):
    repo=make_repo(tmp_path)
    sid=session(client,repo)
    first=capture(client,sid)
    assert first['file_count']==3 and first['status']=='complete'
    assert first['languages']==['dockerfile','python','text']
    assert first['stale'] is False
    assert '不是原子' in first['limitations'][0] and 'Git commit' in first['limitations'][0]
    expected=[]
    total=0
    for item in first['files']:
        raw=(repo/item['path']).read_bytes()
        assert item['bytes']==len(raw)
        assert item['sha256']==hashlib.sha256(raw).hexdigest()
        assert item['lines']==len(raw.decode().splitlines())
        expected.append([item['path'],item['sha256']])
        total+=len(raw)
    assert first['total_bytes']==total
    assert first['digest']==hashlib.sha256(json.dumps(expected,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    assert capture(client,sid)['digest']==first['digest']
    (repo/'app.py').write_text('changed\n')
    latest=capture(client,sid)
    assert latest['digest']!=first['digest']
    detail=client.get(endpoint(sid)+'/'+first['id']).json()
    assert detail==first
    original=client.get(endpoint(sid)+'/'+first['id']+'/file',params={'path':'app.py'}).json()
    assert original['content']=='first\r\nsecond\n第三行\n'
    assert original['line_start']==1 and original['snapshot_id']==first['id']
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM research_findings').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM facts WHERE project_id=?',(sid,)).fetchone()[0]==2
        for sql in ("UPDATE research_source_snapshots SET digest='changed'","UPDATE research_source_files SET content='changed'"):
            with pytest.raises(sqlite3.IntegrityError,match='immutable'): conn.execute(sql)


def test_gets_never_scan_and_cross_project_paths_exact(client,tmp_path,monkeypatch):
    repo=make_repo(tmp_path)
    sid=session(client,repo)
    other=session(client)
    snapshot=capture(client,sid)
    repo.rename(tmp_path/'moved-repo')
    def never(*args,**kwargs): raise AssertionError('GET must not scan files')
    monkeypatch.setattr(sources,'capture_directory',never)
    assert client.get(endpoint(sid)).json()['items'][0]['id']==snapshot['id']
    assert client.get(endpoint(sid)+'/'+snapshot['id']).status_code==200
    url=endpoint(sid)+'/'+snapshot['id']+'/file'
    assert client.get(url,params={'path':'app.py'}).status_code==200
    for path in ('../app.py','/etc/passwd','./app.py','missing.py'):
        assert client.get(url,params={'path':path}).status_code==404
    assert client.get(endpoint(other)+'/'+snapshot['id']).status_code==404
    assert client.get(endpoint(other)+'/'+snapshot['id']+'/file',params={'path':'app.py'}).status_code==404
    assert client.get(endpoint('missing')).status_code==404


def test_skips_hidden_dependencies_links_special_and_binary(client,tmp_path):
    repo=make_repo(tmp_path)
    (repo/'.env').write_text('fixture-hidden-secret')
    for name in ('node_modules','.git','.venv','venv','vendor','build','dist','__pycache__'):
        (repo/name).mkdir()
        (repo/name/'ignored.py').write_text('ignored')
    outside=tmp_path/'outside-secret.txt'
    outside.write_text('outside-source-fixture-only')
    (repo/'symlink.txt').symlink_to(outside)
    os.link(outside,repo/'hardlink.txt')
    os.mkfifo(repo/'fifo')
    (repo/'bad.bin').write_bytes(b'\xff\xfe')
    (repo/'nul.txt').write_bytes(b'abc\x00def')
    sid=session(client,repo)
    snapshot=capture(client,sid)
    assert snapshot['status']=='partial'
    assert snapshot['file_count']==3
    omitted={item['path']:item['reason'] for item in snapshot['omissions']}
    assert set(omitted)>={'.env','.git','.venv','node_modules','venv','vendor','build','dist','__pycache__','symlink.txt','hardlink.txt','fifo','bad.bin','nul.txt'}
    assert '符号链接' in omitted['symlink.txt'] and '硬链接' in omitted['hardlink.txt']
    with db.get_conn() as conn:
        saved=''.join(row[0] for row in conn.execute('SELECT content FROM research_source_files'))
    assert 'fixture-hidden-secret' not in saved and 'outside-source-fixture-only' not in saved


@pytest.mark.parametrize('setting,value,expected',[
    ('max_file_bytes',4,'单文件大小'),
    ('max_total_bytes',4,'总大小'),
    ('max_files',1,'文件数量'),
    ('max_entries',1,'条目数量'),
    ('max_seconds',0,'时间上限'),
])
def test_limits_are_partial_and_explained(client,tmp_path,monkeypatch,setting,value,expected):
    repo=make_repo(tmp_path)
    sid=session(client,repo)
    monkeypatch.setattr(sources,'LIMITS',{**sources.LIMITS,setting:value})
    result=capture(client,sid)
    assert result['status']=='partial'
    assert expected in json.dumps(result['omissions'],ensure_ascii=False)
    assert result['limits'][setting]==value
    if setting=='max_files': assert result['file_count']==1
    if setting=='max_entries': assert result['file_count']==1


def test_depth_and_omission_caps(client,tmp_path,monkeypatch):
    repo=make_repo(tmp_path)
    (repo/'nested').mkdir()
    (repo/'nested'/'x.py').write_text('x')
    for i in range(4): (repo/('.hidden'+str(i))).write_text('skip')
    sid=session(client,repo)
    monkeypatch.setattr(sources,'LIMITS',{**sources.LIMITS,'max_depth':0,'max_omissions':2})
    snapshot=capture(client,sid)
    assert len(snapshot['omissions'])==2
    assert '跳过详情达到显示上限' in ''.join(snapshot['limitations'])
    assert not any(item['path']=='nested/x.py' for item in snapshot['files'])
    assert snapshot['status']=='partial'


def test_capture_rechecks_authorization_and_has_no_db_write_lock(client,tmp_path,monkeypatch):
    repo=make_repo(tmp_path)
    sid=session(client,repo)
    original=sources.capture_directory
    def changed(path):
        # This concurrent writer must succeed while filesystem capture is outside a transaction.
        with db.get_conn() as conn:
            conn.execute('UPDATE research_sessions SET authorization_revision=authorization_revision+1 WHERE id=?',(sid,))
        return original(path)
    monkeypatch.setattr(sources,'capture_directory',changed)
    response=client.post(endpoint(sid))
    assert response.status_code==409
    assert client.get(endpoint(sid)).json()['items']==[]
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM research_source_files').fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM research_events WHERE kind='source'").fetchone()[0]==0


def test_running_no_repo_and_csrf(client,tmp_path):
    repo=make_repo(tmp_path)
    empty=session(client)
    assert client.post(endpoint(empty)).status_code==409
    sid=session(client,repo)
    for status in ('running','pause_requested'):
        with db.get_conn() as conn: conn.execute('UPDATE research_sessions SET status=? WHERE id=?',(status,sid))
        assert client.post(endpoint(sid)).status_code==409
    assert client.post(endpoint(sid),headers={'Origin':'http://other.invalid'}).status_code==403


def test_root_ancestor_symlink_and_mount_boundary(client,tmp_path,monkeypatch):
    parent=tmp_path/'parent'
    parent.mkdir()
    repo=parent/'repo'
    repo.mkdir()
    (repo/'a.py').write_text('safe')
    sid=session(client,repo)
    parent.rename(tmp_path/'original-parent')
    parent.symlink_to(tmp_path/'original-parent',target_is_directory=True)
    assert client.post(endpoint(sid)).status_code==422
    parent.unlink()
    (tmp_path/'original-parent').rename(parent)
    mount=repo/'mount'
    mount.mkdir()
    (mount/'excluded.py').write_text('excluded')
    real_mount_id=sources._mount_id
    def simulated_mount(fd):
        actual=real_mount_id(fd)
        return actual+100 if os.readlink('/proc/self/fd/'+str(fd)).endswith('/mount') else actual
    monkeypatch.setattr(sources,'_mount_id',simulated_mount)
    result=capture(client,sid)
    assert result['file_count']==1
    assert any('挂载边界' in item['reason'] for item in result['omissions'])


def test_file_change_during_read_is_omitted(client,tmp_path,monkeypatch):
    repo=tmp_path/'repo'
    repo.mkdir()
    source=repo/'changing.py'
    source.write_text('before')
    sid=session(client,repo)
    real_read=sources.os.read
    changed=False
    def mutate(fd,size):
        nonlocal changed
        result=real_read(fd,size)
        if not changed and os.readlink('/proc/self/fd/'+str(fd)).endswith('changing.py'):
            changed=True
            source.write_text('after-content')
        return result
    monkeypatch.setattr(sources.os,'read',mutate)
    result=capture(client,sid)
    assert changed and result['file_count']==0 and result['status']=='partial'
    assert any('发生变化' in item['reason'] for item in result['omissions'])


def test_stale_and_old_snapshot_line_evidence_preserves_crlf(client,tmp_path):
    repo=make_repo(tmp_path)
    sid=session(client,repo)
    snapshot=capture(client,sid)
    other=tmp_path/'other'
    other.mkdir()
    client.patch('/api/research/sessions/'+sid+'/materials',json={'repo':str(other),'authorization_confirmed':True})
    assert client.get(endpoint(sid)).json()['items'][0]['stale'] is True
    response=client.post(endpoint(sid)+'/'+snapshot['id']+'/evidence',json={'path':'app.py','start_line':1,'end_line':2})
    assert response.status_code==201,response.text
    evidence_id=response.json()['evidence_id']
    detail=client.get('/api/research/sessions/'+sid).json()
    assert detail['findings']==[]
    evidence=next(item for item in detail['evidence'] if item['id']==evidence_id)
    assert evidence['content']=='first\r\nsecond\n'
    metadata=evidence['metadata']
    assert metadata['snapshot_id']==snapshot['id'] and metadata['snapshot_digest']==snapshot['digest']
    assert metadata['repo']==str(repo) and metadata['line_start']==1 and metadata['line_end']==2
    assert metadata['fragment_sha256']==hashlib.sha256(evidence['content'].encode()).hexdigest()
    assert metadata['sha256']==next(item['sha256'] for item in snapshot['files'] if item['path']=='app.py')
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM facts WHERE project_id=?',(sid,)).fetchone()[0]==2


@pytest.mark.parametrize('start,end',[(0,1),(2,1),(1,4),(1,401),(1.1,2),(True,2)])
def test_invalid_evidence_selection(client,tmp_path,start,end):
    repo=make_repo(tmp_path)
    sid=session(client,repo)
    snapshot=capture(client,sid)
    response=client.post(endpoint(sid)+'/'+snapshot['id']+'/evidence',json={'path':'app.py','start_line':start,'end_line':end})
    assert response.status_code==422
    assert client.get('/api/research/sessions/'+sid).json()['evidence']==[]


def test_evidence_scope_status_and_empty_file(client,tmp_path):
    repo=make_repo(tmp_path)
    (repo/'empty').touch()
    sid=session(client,repo)
    other=session(client)
    snapshot=capture(client,sid)
    body={'path':'app.py','start_line':1,'end_line':1}
    assert client.post(endpoint(other)+'/'+snapshot['id']+'/evidence',json=body).status_code==404
    assert client.post(endpoint(sid)+'/'+snapshot['id']+'/evidence',json={**body,'path':'empty'}).status_code==422
    for status in ('running','pause_requested'):
        with db.get_conn() as conn: conn.execute('UPDATE research_sessions SET status=? WHERE id=?',(status,sid))
        assert client.post(endpoint(sid)+'/'+snapshot['id']+'/evidence',json=body).status_code==409


def test_same_second_snapshot_order_is_creation_order(client,tmp_path,monkeypatch):
    repo=make_repo(tmp_path)
    sid=session(client,repo)
    monkeypatch.setattr(sources,'utcnow',lambda:'2026-09-09T01:02:03Z')
    created=[capture(client,sid)['id'] for _ in range(4)]
    listed=client.get(endpoint(sid)).json()['items']
    assert [item['id'] for item in listed]==list(reversed(created))
