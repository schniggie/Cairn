"""Only synthetic local files; no target access and no user credentials."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import stat

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.server import db, research_identities as identities
from cairn.server.routers.research import router as research_router
from cairn.server.routers.research_identities import router

SECRET='fixture-only-SECRET-7f2ae310'
USERNAME='fixture-only-USER-54e892'


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'_db_path',None)
    monkeypatch.setattr(identities,'IMPORT_DIRECTORY',tmp_path/'imports')
    monkeypatch.setattr(identities,'KEY_DIRECTORY',tmp_path/'vault')
    db.configure(tmp_path/'research.db')
    app=FastAPI()
    app.include_router(research_router)
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def session(client,**patch):
    body={'title':'Fixture project','objective':'Store fixture configuration','url':'http://example.invalid:81/path','authorization_confirmed':True}
    body.update(patch)
    response=client.post('/api/research/sessions',json=body)
    assert response.status_code==201,response.text
    return response.json()['id']


def endpoint(sid):
    return '/api/research/sessions/'+sid+'/identities'


def source(name='account.json',data=None,raw=None,mode=0o600):
    identities.IMPORT_DIRECTORY.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=identities.IMPORT_DIRECTORY/name
    if raw is None:
        raw=json.dumps(data or {'type':'account','username':USERNAME,'password':SECRET}).encode()
    path.write_bytes(raw)
    path.chmod(mode)
    return name


def imported(client,sid,name='account.json'):
    response=client.post(endpoint(sid),json={'label':'Reader role','source_file':name})
    assert response.status_code==201,response.text
    return response.json()


def test_metadata_encryption_aad_and_no_secret_leaks(client):
    sid=session(client)
    listing=client.get(endpoint(sid)).json()
    assert listing['items']==[] and listing['formats']==['account','basic','headers','cookies']
    assert listing['import_directory']==str(identities.IMPORT_DIRECTORY)
    assert stat.S_IMODE(identities.IMPORT_DIRECTORY.stat().st_mode)==0o700
    assert not identities.KEY_DIRECTORY.exists()
    source()
    meta=imported(client,sid)
    assert set(meta)==set(identities.METADATA_FIELDS)
    assert meta['kind']=='account' and meta['origin']=='http://example.invalid:81' and meta['version']==1
    key_path=identities.KEY_DIRECTORY/'master.key'
    key=key_path.read_bytes()
    assert len(key)==32 and stat.S_IMODE(key_path.stat().st_mode)==0o600
    assert stat.S_IMODE(identities.KEY_DIRECTORY.stat().st_mode)==0o700
    with db.get_conn() as conn:
        row=conn.execute('SELECT * FROM research_identities WHERE id=?',(meta['id'],)).fetchone()
        assert SECRET.encode() not in row['ciphertext'] and USERNAME.encode() not in row['ciphertext']
        aad=json.dumps([sid,meta['id'],1,meta['origin']],separators=(',',':')).encode()
        recovered=json.loads(AESGCM(key).decrypt(row['nonce'],row['ciphertext'],aad))
        assert recovered=={'type':'account','username':USERNAME,'password':SECRET}
        with pytest.raises(InvalidTag): AESGCM(key).decrypt(row['nonce'],row['ciphertext'],b'wrong-project-aad')
        dump='\n'.join(conn.iterdump())
        assert SECRET not in dump and USERNAME not in dump
    for url in (endpoint(sid),'/api/research/sessions/'+sid,'/api/research/sessions/'+sid+'/report.md'):
        response=client.get(url)
        assert SECRET not in response.text and USERNAME not in response.text
    report=client.post('/api/research/sessions/'+sid+'/reports')
    assert SECRET not in report.text and USERNAME not in report.text
    assert (identities.IMPORT_DIRECTORY/'account.json').exists()
    for suffix in ('','-wal','-shm'):
        path=Path(str(db._db_path)+suffix)
        if path.exists():
            assert SECRET.encode() not in path.read_bytes() and USERNAME.encode() not in path.read_bytes()
    assert client.get(endpoint(sid)+'/'+meta['id']).status_code==405
    assert client.get(endpoint(sid)+'/'+meta['id']+'/plaintext').status_code==404


@pytest.mark.parametrize('kind,fields',[
    ('account',{'username':USERNAME,'password':SECRET}),
    ('basic',{'username':USERNAME,'password':SECRET}),
    ('headers',{'headers':{'Authorization':'Bearer '+SECRET}}),
    ('cookies',{'cookies':{'session':SECRET}}),
])
def test_supported_formats(client,kind,fields):
    sid=session(client)
    source(data={'type':kind,**fields})
    assert imported(client,sid)['kind']==kind


def test_rotation_stale_revoke_and_reimport(client,tmp_path):
    sid=session(client,repo=str(tmp_path))
    source()
    first=imported(client,sid)
    item_url=endpoint(sid)+'/'+first['id']
    with db.get_conn() as conn:
        old=bytes(conn.execute('SELECT ciphertext FROM research_identities WHERE id=?',(first['id'],)).fetchone()[0])
    source('replacement.json',data={'type':'cookies','cookies':{'session':'replacement-'+SECRET}})
    second=client.post(item_url+'/replace',json={'source_file':'replacement.json'}).json()
    assert second['version']==2 and second['kind']=='cookies'
    with db.get_conn() as conn:
        assert conn.execute('SELECT ciphertext FROM research_identities WHERE id=?',(first['id'],)).fetchone()[0]!=old
    client.patch('/api/research/sessions/'+sid+'/materials',json={'url':'https://different.invalid/','authorization_confirmed':True})
    assert client.get(endpoint(sid)).json()['items'][0]['status']=='stale'
    third=client.post(item_url+'/replace',json={'source_file':'account.json'}).json()
    assert third['version']==3 and third['origin']=='https://different.invalid' and third['status']=='active'
    client.patch('/api/research/sessions/'+sid+'/materials',json={'url':None})
    assert client.get(endpoint(sid)).json()['items'][0]['status']=='stale'
    revoked=client.delete(item_url).json()
    assert revoked['status']=='revoked'
    assert client.delete(item_url).json()==revoked
    with db.get_conn() as conn:
        row=conn.execute('SELECT ciphertext,nonce FROM research_identities WHERE id=?',(first['id'],)).fetchone()
        assert tuple(row)==(None,None)
    assert client.post(item_url+'/replace',json={'source_file':'account.json'}).status_code==409
    client.patch('/api/research/sessions/'+sid+'/materials',json={'url':'https://different.invalid/','authorization_confirmed':True})
    restored=client.post(item_url+'/replace',json={'source_file':'account.json'}).json()
    assert restored['version']==4 and restored['status']=='active'


def test_cross_project_running_no_url_and_csrf(client,tmp_path):
    sid=session(client)
    other=session(client)
    code=session(client,url=None,repo=str(tmp_path))
    source()
    meta=imported(client,sid)
    wrong=endpoint(other)+'/'+meta['id']
    assert client.post(wrong+'/replace',json={'source_file':'account.json'}).status_code==404
    assert client.delete(wrong).status_code==404
    assert client.get(endpoint(other)).json()['items']==[]
    assert client.post(endpoint(code),json={'label':'r','source_file':'account.json'}).status_code==409
    for state in ('running','pause_requested'):
        with db.get_conn() as conn: conn.execute('UPDATE research_sessions SET status=? WHERE id=?',(state,sid))
        assert client.post(endpoint(sid),json={'label':'r','source_file':'account.json'}).status_code==409
        assert client.post(endpoint(sid)+'/'+meta['id']+'/replace',json={'source_file':'account.json'}).status_code==409
        assert client.delete(endpoint(sid)+'/'+meta['id']).status_code==409
    assert client.post(endpoint(other),json={'label':'r','source_file':'account.json'},headers={'Origin':'http://evil.invalid'}).status_code==403
    assert client.get(endpoint('missing')).status_code==404


@pytest.mark.parametrize('name',['../account.json','/tmp/account.json','account.txt','..json','folder/account.json','x\\account.json'])
def test_basename_only(client,name):
    sid=session(client)
    response=client.post(endpoint(sid),json={'label':'r','source_file':name})
    assert response.status_code==422
    assert not identities.KEY_DIRECTORY.exists()


def test_symlink_hardlink_permissions_oversize_and_fifo_rejected(client):
    sid=session(client)
    source()
    directory=identities.IMPORT_DIRECTORY
    (directory/'symlink.json').symlink_to(directory/'account.json')
    os.link(directory/'account.json',directory/'hardlink.json')
    for name in ('symlink.json','hardlink.json','account.json'):
        assert client.post(endpoint(sid),json={'label':'r','source_file':name}).status_code==422
    (directory/'hardlink.json').unlink()
    source('permissive.json',mode=0o644)
    source('oversize.json',raw=b'x'*65537)
    os.mkfifo(directory/'fifo.json',0o600)
    for name in ('permissive.json','oversize.json','fifo.json'):
        assert client.post(endpoint(sid),json={'label':'r','source_file':name}).status_code==422
    assert client.get(endpoint(sid)).json()['items']==[]
    assert not identities.KEY_DIRECTORY.exists()


def test_directory_permissions_and_symlink_rejected(client,tmp_path,monkeypatch):
    sid=session(client)
    source()
    identities.IMPORT_DIRECTORY.chmod(0o755)
    assert client.get(endpoint(sid)).status_code==503
    identities.IMPORT_DIRECTORY.chmod(0o700)
    link=tmp_path/'linked-imports'
    link.symlink_to(identities.IMPORT_DIRECTORY,target_is_directory=True)
    monkeypatch.setattr(identities,'IMPORT_DIRECTORY',link)
    assert client.post(endpoint(sid),json={'label':'r','source_file':'account.json'}).status_code==503


@pytest.mark.parametrize('raw',[
    ('{"type":"account","username":"'+USERNAME+'","password":"'+SECRET+'","extra":1}').encode(),
    ('{"type":"account","username":"'+USERNAME+'","password":"'+SECRET+'","password":"again"}').encode(),
    ('{"type":"headers","headers":{"X-Token":"'+SECRET+'\\r\\nBad"}}').encode(),
    ('{"type":"cookies","cookies":{"session":"'+SECRET+'; next=1"}}').encode(),
    ('{invalid '+SECRET).encode(),
    b'\xff\xfe',
])
def test_invalid_files_errors_do_not_echo_inputs(client,raw):
    sid=session(client)
    source(raw=raw)
    response=client.post(endpoint(sid),json={'label':'r','source_file':'account.json'})
    assert response.status_code==422,response.text
    assert SECRET not in response.text and USERNAME not in response.text
    assert client.get(endpoint(sid)).json()['items']==[]
    assert not identities.KEY_DIRECTORY.exists()


def test_invalid_request_does_not_echo_secret(client):
    sid=session(client)
    for body in ({'label':'r','source_file':'account.json','password':SECRET},{'source_file':{'password':SECRET},'label':'r'}):
        response=client.post(endpoint(sid),json=body)
        assert response.status_code==422 and SECRET not in response.text
    response=client.post(endpoint(sid),content=SECRET*200)
    assert response.status_code==422 and SECRET not in response.text


def test_lost_or_changed_key_never_silently_replaced(client):
    sid=session(client)
    source()
    meta=imported(client,sid)
    key_path=identities.KEY_DIRECTORY/'master.key'
    original=key_path.read_bytes()
    key_path.unlink()
    response=client.post(endpoint(sid)+'/'+meta['id']+'/replace',json={'source_file':'account.json'})
    assert response.status_code==503 and not key_path.exists()
    key_path.write_bytes(b'X'*32)
    key_path.chmod(0o600)
    assert client.post(endpoint(sid),json={'label':'r','source_file':'account.json'}).status_code==503
    key_path.write_bytes(original)
    assert client.post(endpoint(sid)+'/'+meta['id']+'/replace',json={'source_file':'account.json'}).status_code==200


def test_key_creation_concurrency_uses_one_valid_key(client):
    sid=session(client)
    source()
    def save(number): return identities.import_identity(sid,'Role '+str(number),'account.json')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(save,range(4)))
    key=(identities.KEY_DIRECTORY/'master.key').read_bytes()
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM research_identity_keys').fetchone()[0]==1
        for meta in results:
            row=conn.execute('SELECT * FROM research_identities WHERE id=?',(meta['id'],)).fetchone()
            aad=json.dumps([sid,meta['id'],1,meta['origin']],separators=(',',':')).encode()
            assert json.loads(AESGCM(key).decrypt(row['nonce'],row['ciphertext'],aad))['password']==SECRET


def test_private_file_owner_and_key_permissions_checked(client,monkeypatch):
    sid=session(client)
    source()
    actual_fstat=os.fstat
    def other_owner(fd):
        result=actual_fstat(fd)
        if stat.S_ISREG(result.st_mode) and result.st_size>32:
            values=list(result)
            values[4]=os.getuid()+1
            return os.stat_result(values)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(identities.os,'fstat',other_owner)
        assert client.post(endpoint(sid),json={'label':'r','source_file':'account.json'}).status_code==422
    imported(client,sid)
    key=identities.KEY_DIRECTORY/'master.key'
    key.chmod(0o644)
    assert client.post(endpoint(sid),json={'label':'r','source_file':'account.json'}).status_code==503
    key.chmod(0o600)
    lock=identities.KEY_DIRECTORY/'master.lock'
    lock.unlink()
    lock.symlink_to(key)
    assert client.post(endpoint(sid),json={'label':'r','source_file':'account.json'}).status_code==503


def test_failed_replacement_keeps_previous_ciphertext_and_version(client):
    sid=session(client)
    source()
    original=imported(client,sid)
    with db.get_conn() as conn:
        before=dict(conn.execute('SELECT * FROM research_identities WHERE id=?',(original['id'],)).fetchone())
    source('broken.json',raw=b'not JSON '+SECRET.encode())
    response=client.post(endpoint(sid)+'/'+original['id']+'/replace',json={'source_file':'broken.json'})
    assert response.status_code==422 and SECRET not in response.text
    with db.get_conn() as conn:
        after=dict(conn.execute('SELECT * FROM research_identities WHERE id=?',(original['id'],)).fetchone())
    assert after==before


@pytest.mark.parametrize('url,expected',[
    ('http://example.invalid:0/path','http://example.invalid:0'),
    ('http://example.invalid:80/path','http://example.invalid'),
    ('https://example.invalid:443/path','https://example.invalid'),
    ('https://example.invalid:444/path','https://example.invalid:444'),
])
def test_identity_origin_preserves_nondefault_port(client,url,expected):
    sid=session(client,url=url)
    source()
    item=imported(client,sid)
    assert item['origin']==expected
    assert client.get(endpoint(sid)).json()['items'][0]['status']=='active'
