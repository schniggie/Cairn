"""Comparison fixtures are stored text in temporary SQLite databases only."""
from contextlib import contextmanager
import hashlib
import json
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.server import db, research_source_compare as compare
from cairn.server.routers.research import router as research_router
from cairn.server.routers.research_source_compare import router


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'_db_path',None)
    db.configure(tmp_path/'comparison.db')
    app=FastAPI()
    app.include_router(research_router)
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def project(client):
    response=client.post('/api/research/sessions',json={'title':'Comparison fixture','objective':'Compare saved text','url':'http://example.invalid','authorization_confirmed':True})
    assert response.status_code==201
    return response.json()['id']


def snapshot(sid,files,repo='/fixture/unread-directory',status='complete'):
    identity='source-'+uuid4().hex
    rows=[(path,text,hashlib.sha256(text.encode()).hexdigest()) for path,text in sorted(files.items())]
    digest=hashlib.sha256(json.dumps([[path,sha] for path,_,sha in rows]).encode()).hexdigest()
    with db.get_conn() as conn:
        conn.execute('INSERT INTO research_source_snapshots (id,session_id,repo,authorization_revision,created_at,digest,file_count,total_bytes,languages_json,status,limitations_json,omissions_json,limits_json) VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?)',
            (identity,sid,repo,'2026-09-09T00:00:00Z',digest,len(files),sum(len(text.encode()) for text in files.values()),'["text"]',status,'[]','[]','{}'))
        conn.executemany('INSERT INTO research_source_files (snapshot_id,path,content,sha256,bytes,lines,language) VALUES (?,?,?,?,?,?,?)',
            [(identity,path,text,sha,len(text.encode()),len(text.splitlines()),'text') for path,text,sha in rows])
    return identity


def request(client,sid,before,after,path=None):
    url='/api/research/sessions/'+sid+'/source-comparison'
    params={'before':before,'after':after}
    if path is not None:
        url+='/file'
        params['path']=path
    return client.get(url,params=params)


def test_changed_counts_sorted_paths_and_no_rename_inference(client):
    sid=project(client)
    before=snapshot(sid,{'unchanged':'same\n','b_removed':'old\n','c_modified':'first\n','renamed_old':'identical\n'})
    after=snapshot(sid,{'unchanged':'same\n','a_added':'new\n','c_modified':'second\n','renamed_new':'identical\n'})
    response=request(client,sid,before,after)
    assert response.status_code==200,response.text
    data=response.json()
    assert data['counts']=={'added':2,'removed':2,'modified':1,'unchanged':1}
    assert [item['path'] for item in data['files']]==['a_added','b_removed','c_modified','renamed_new','renamed_old']
    assert 'unchanged' not in [item['change'] for item in data['files']]
    assert data['files'][0]['before_sha256'] is None
    assert data['files'][1]['after_sha256'] is None
    assert '不能据此断言' in ''.join(data['limitations'])
    assert '不推断重命名' in ''.join(data['limitations'])
    assert data['before']['id']==before and data['after']['id']==after


def test_partial_and_different_repositories_explicit(client):
    sid=project(client)
    before=snapshot(sid,{'app.py':'old'},repo='/fixture/a',status='partial')
    after=snapshot(sid,{'app.py':'new'},repo='/fixture/b')
    data=request(client,sid,before,after).json()
    assert data['counts']['modified']==1
    assert '部分材料' in ''.join(data['limitations']) and '不同代码目录' in ''.join(data['limitations'])
    file=request(client,sid,before,after,'app.py').json()
    assert '部分材料' in ''.join(file['limitations']) and '不同代码目录' in ''.join(file['limitations'])


def test_unified_line_numbers_context_and_html_as_text(client):
    sid=project(client)
    before=snapshot(sid,{'app.py':'first\nold\ntail\n'})
    after=snapshot(sid,{'app.py':'first\n<script>untrusted fixture</script>\ntail\n'})
    data=request(client,sid,before,after,'app.py').json()
    assert data['change']=='modified' and not data['truncated']
    assert data['lines']==[
        {'kind':'hunk','before_line':None,'after_line':None,'text':'@@ -1,3 +1,3 @@','eol':None},
        {'kind':'context','before_line':1,'after_line':1,'text':'first','eol':'lf'},
        {'kind':'removed','before_line':2,'after_line':None,'text':'old','eol':'lf'},
        {'kind':'added','before_line':None,'after_line':2,'text':'<script>untrusted fixture</script>','eol':'lf'},
        {'kind':'context','before_line':3,'after_line':3,'text':'tail','eol':'lf'},
    ]


def test_appearance_absence_and_empty_are_distinct(client):
    sid=project(client)
    before=snapshot(sid,{'removed':'first\nsecond'})
    after=snapshot(sid,{'added':'value\n','empty':''})
    added=request(client,sid,before,after,'added').json()
    assert added['change']=='added' and added['before_sha256'] is None
    assert added['lines'][0]['text']=='@@ -0,0 +1 @@'
    assert added['lines'][1]['after_line']==1
    removed=request(client,sid,before,after,'removed').json()
    assert removed['change']=='removed' and removed['after_sha256'] is None
    assert [line['before_line'] for line in removed['lines'] if line['kind']=='removed']==[1,2]
    empty=request(client,sid,before,after,'empty').json()
    assert empty['change']=='added' and empty['lines']==[] and not empty['truncated']
    assert '空文件' in ''.join(empty['limitations'])


@pytest.mark.parametrize('old,new,old_eol,new_eol',[
    ('line\r\n','line\n','crlf','lf'),
    ('line\n','line','lf','none'),
    ('line\r','line\n','cr','lf'),
    ('line\u2028','line\n','other','lf'),
])
def test_newline_only_changes_are_not_hidden(client,old,new,old_eol,new_eol):
    sid=project(client)
    before=snapshot(sid,{'x':old})
    after=snapshot(sid,{'x':new})
    data=request(client,sid,before,after,'x').json()
    assert data['change']=='modified' and not data['truncated']
    removed=next(item for item in data['lines'] if item['kind']=='removed')
    added=next(item for item in data['lines'] if item['kind']=='added')
    assert removed['text']==added['text']=='line'
    assert removed['eol']==old_eol and added['eol']==new_eol
    assert '差异仅在换行' in ''.join(data['limitations'])


def test_same_snapshot_and_equal_content_have_no_diff(client):
    sid=project(client)
    before=snapshot(sid,{'x':'same\n'})
    after=snapshot(sid,{'x':'same\n'})
    for other in (before,after):
        data=request(client,sid,before,other).json()
        assert data['counts']=={'added':0,'removed':0,'modified':0,'unchanged':1}
        assert data['files']==[]
        detail=request(client,sid,before,other,'x').json()
        assert detail['change']=='unchanged' and detail['lines']==[] and not detail['truncated']
        assert '摘要相同' in ''.join(detail['limitations'])


def test_cross_project_missing_paths_and_exact_path_matching(client):
    sid=project(client)
    other=project(client)
    before=snapshot(sid,{'a':'original'})
    after=snapshot(other,{'a':'different'})
    assert request(client,sid,before,after).status_code==404
    assert request(client,sid,after,before).status_code==404
    assert request(client,sid,before,after,'a').status_code==404
    assert request(client,other,before,after,'a').status_code==404
    assert request(client,'missing',before,before).status_code==404
    for path in ('../a','/etc/passwd','./a','missing'):
        assert request(client,sid,before,before,path).status_code==404


@pytest.mark.parametrize('text',[
    'a'*(compare.MAX_SIDE_BYTES+1),
    'a\n'*(compare.MAX_SIDE_LINES+1),
    'a'*(compare.MAX_LINE_CHARACTERS+1),
])
def test_oversized_inputs_not_misreported_as_equal(client,text):
    sid=project(client)
    before=snapshot(sid,{'x':text})
    after=snapshot(sid,{'x':'different'})
    data=request(client,sid,before,after,'x').json()
    assert data['change']=='modified' and data['truncated'] and data['lines']==[]
    assert '未计算行差异' in ''.join(data['limitations'])


def test_output_limit_and_repetitive_inputs_stay_bounded(client,monkeypatch):
    sid=project(client)
    before=snapshot(sid,{'x':'old\n'*1000})
    after=snapshot(sid,{'x':'new\n'*1000})
    data=request(client,sid,before,after,'x').json()
    assert data['truncated'] and len(data['lines'])==compare.MAX_OUTPUT_LINES
    assert '剩余差异未展示' in ''.join(data['limitations'])
    assert data['change']=='modified'


def test_large_file_skips_loading_content(client,monkeypatch):
    sid=project(client)
    before=snapshot(sid,{'x':'a'*(compare.MAX_SIDE_BYTES+1)})
    after=snapshot(sid,{'x':'different'})
    statements=[]
    @contextmanager
    def traced_conn():
        with db.get_conn() as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(compare,'get_conn',traced_conn)
    assert request(client,sid,before,after,'x').json()['truncated']
    assert not any('SELECT content FROM' in sql for sql in statements)


def test_read_transaction_has_consistent_project_context_and_no_mutations(client,monkeypatch):
    sid=project(client)
    before=snapshot(sid,{'x':'old'})
    after=snapshot(sid,{'x':'new'})
    injected=False
    queries=[]
    @contextmanager
    def traced_conn():
        with db.get_conn() as conn:
            def during_select(statement):
                nonlocal injected
                queries.append(statement)
                if 'SELECT * FROM research_source_snapshots' in statement and not injected:
                    injected=True
                    with db.get_conn() as writer:
                        writer.execute('UPDATE research_sessions SET repo=? WHERE id=?',('/fixture/unread-directory',sid))
            conn.set_trace_callback(during_select)
            yield conn
    monkeypatch.setattr(compare,'get_conn',traced_conn)
    with db.get_conn() as conn:
        event_count=conn.execute('SELECT count(*) FROM research_events').fetchone()[0]
    result=request(client,sid,before,after).json()
    assert injected
    assert result['before']['stale'] and result['after']['stale']
    assert 'BEGIN' in queries and 'PRAGMA query_only=ON' in queries
    assert not any(sql.lstrip().upper().startswith(('UPDATE ','INSERT ','DELETE ')) for sql in queries)
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM research_events').fetchone()[0]==event_count
    assert request(client,sid,before,after).json()['before']['stale'] is False
