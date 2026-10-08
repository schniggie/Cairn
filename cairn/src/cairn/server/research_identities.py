"""Local credential-file import and encrypted storage only.

No authentication attempts, target requests, execution integration, or plaintext
retrieval interface live here. Source files remain under the user's control.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.research_services import append_event
from cairn.server.services import utcnow

IMPORT_DIRECTORY = Path.home() / '.local/share/cairn/identity-imports'
KEY_DIRECTORY = Path.home() / '.local/share/cairn/identity-vault'
MAX_FILE_BYTES = 65536
FORMATS = ['account', 'basic', 'headers', 'cookies']
METADATA_FIELDS = ('id','label','kind','origin','version','status','created_at','updated_at')
TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _private_directory(path: Path) -> int:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise HTTPException(503, '身份材料目录必须由当前服务用户拥有，权限为 700')
        return fd
    except OSError:
        raise HTTPException(503, '无法安全打开身份材料私有目录') from None


def _safe_regular(fd: int, *, max_size: int, exact_size: int | None = None):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > max_size or (exact_size is not None and info.st_size != exact_size)):
        raise HTTPException(422, '导入文件必须是当前用户拥有的 600 权限单链接普通文件，大小不超过 64 KiB')
    return info


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate key')
        result[key] = value
    return result


def _credential_json(raw: bytes):
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        if not isinstance(data, dict): raise ValueError()
        kind = data.get('type')
        expected = {'account': {'type','username','password'}, 'basic': {'type','username','password'}, 'headers': {'type','headers'}, 'cookies': {'type','cookies'}}
        if not isinstance(kind, str) or kind not in expected or set(data) != expected[kind]: raise ValueError()
        def clean(value, *, nonempty=True):
            return (isinstance(value,str) and (bool(value) or not nonempty) and len(value)<=16384
                and not any(ord(c)<32 or ord(c)==127 for c in value))
        if kind in ('account','basic'):
            if not clean(data['username']) or not clean(data['password'],nonempty=False): raise ValueError()
        else:
            values = data[kind]
            if not isinstance(values,dict) or not 1 <= len(values) <= 64: raise ValueError()
            for name,value in values.items():
                if not isinstance(name,str) or len(name)>128 or not TOKEN.fullmatch(name) or not clean(value): raise ValueError()
                if kind == 'cookies' and (';' in value or ',' in value): raise ValueError()
        return kind, json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise HTTPException(422, '身份文件格式无效；请使用 account、basic、headers 或 cookies 的严格 JSON 格式') from None


def validate_source_file(source_file):
    if (not isinstance(source_file,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.json',source_file)
        or '..' in source_file):
        raise HTTPException(422, '只接受私有导入目录内的 JSON 文件名，不接受路径')
    return source_file


def read_import(source_file):
    validate_source_file(source_file)
    directory_fd = _private_directory(IMPORT_DIRECTORY)
    fd = None
    try:
        fd = os.open(source_file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
        before = _safe_regular(fd,max_size=MAX_FILE_BYTES)
        chunks, total = [], 0
        while total <= MAX_FILE_BYTES:
            chunk = os.read(fd,min(8192,MAX_FILE_BYTES+1-total))
            if not chunk: break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if total > MAX_FILE_BYTES or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise HTTPException(422,'身份文件过大或在读取过程中发生变更')
        return _credential_json(b''.join(chunks))
    except OSError:
        raise HTTPException(422,'无法安全读取指定身份文件；请检查文件存在、类型和权限') from None
    finally:
        if fd is not None: os.close(fd)
        os.close(directory_fd)


def _load_key(expected_fingerprint):
    directory_fd = _private_directory(KEY_DIRECTORY)
    lock_fd = key_fd = None
    try:
        # Serialize creation across processes. The lock file itself is subject
        # to the same no-follow/owner/mode checks, before trusting its lock.
        lock_fd = os.open('master.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=directory_fd)
        _safe_regular(lock_fd,max_size=0,exact_size=0)
        fcntl.flock(lock_fd,fcntl.LOCK_EX)
        try:
            key_fd = os.open('master.key',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
        except FileNotFoundError:
            if expected_fingerprint:
                raise HTTPException(503,'身份保管密钥缺失，请恢复原密钥；不会自动替换')
            # O_EXCL also protects against an unexpected non-cooperating creator.
            write_fd = os.open('master.key',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
            try:
                key = os.urandom(32)
                if os.write(write_fd,key)!=32: raise OSError('incomplete key write')
                os.fsync(write_fd)
            finally:
                os.close(write_fd)
            os.fsync(directory_fd)
            key_fd = os.open('master.key',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
        _safe_regular(key_fd,max_size=32,exact_size=32)
        key = os.read(key_fd,33)
        fingerprint = hashlib.sha256(key).hexdigest()
        if len(key)!=32 or (expected_fingerprint and fingerprint!=expected_fingerprint):
            raise HTTPException(503,'身份保管密钥与已登记密钥不一致，请恢复原密钥')
        return key,fingerprint
    except HTTPException as exc:
        if exc.status_code==422:
            raise HTTPException(503,'身份保管密钥或锁文件的类型、所有者、权限或长度无效') from None
        raise
    except OSError:
        raise HTTPException(503,'身份保管密钥无法安全读取或建立') from None
    finally:
        if key_fd is not None: os.close(key_fd)
        if lock_fd is not None: os.close(lock_fd)
        os.close(directory_fd)


def _origin(url):
    if not url: return None
    parsed = urlsplit(url)
    host = parsed.hostname.lower()
    if ':' in host: host = '['+host+']'
    port = parsed.port
    if port is not None and port != (443 if parsed.scheme=='https' else 80): host += ':'+str(port)
    return parsed.scheme+'://'+host


def _session(conn,session_id,*,writable=False):
    row=conn.execute('SELECT id,url,status FROM research_sessions WHERE id=?',(session_id,)).fetchone()
    if not row: raise HTTPException(404,'研究不存在')
    if writable and row['status'] in ('running','pause_requested'):
        raise HTTPException(409,'请先暂停并等待执行器停止，再管理测试身份')
    return row


def _identity(conn,session_id,identity_id):
    row=conn.execute('SELECT * FROM research_identities WHERE session_id=? AND id=?',(session_id,identity_id)).fetchone()
    if not row: raise HTTPException(404,'测试身份不存在')
    return row


def _metadata(row,url):
    result={k:row[k] for k in METADATA_FIELDS}
    if result['status']=='active' and result['origin']!=_origin(url): result['status']='stale'
    return result


def list_identities(session_id):
    # Do not expose a filesystem directory for a nonexistent project.
    with get_conn() as conn:
        conn.execute('BEGIN')
        session=_session(conn,session_id)
        items=[_metadata(row,session['url']) for row in conn.execute('SELECT * FROM research_identities WHERE session_id=? ORDER BY created_at,id',(session_id,))]
    directory_fd=_private_directory(IMPORT_DIRECTORY)
    os.close(directory_fd)
    return {'items':items,'import_directory':str(IMPORT_DIRECTORY),'formats':FORMATS}


def import_identity(session_id,label,source_file,identity_id=None):
    validate_source_file(source_file)
    if identity_id is None and (not isinstance(label,str) or not 1<=len(label.strip())<=100 or any(ord(c)<32 or ord(c)==127 for c in label)):
        raise HTTPException(422,'请填写 1–100 字符的身份标签')
    # All filesystem operations happen outside SQLite write transactions.
    with get_conn() as conn:
        session=_session(conn,session_id,writable=True)
        if not session['url']: raise HTTPException(409,'请先提供网站地址，再导入绑定该目标的测试身份')
        origin=_origin(session['url'])
        existing=_identity(conn,session_id,identity_id) if identity_id else None
        expected=conn.execute('SELECT fingerprint FROM research_identity_keys WHERE id=1').fetchone()
    kind,plaintext=read_import(source_file)
    key,fingerprint=_load_key(expected[0] if expected else None)
    iid=identity_id or 'identity-'+uuid4().hex
    version=existing['version']+1 if existing else 1
    aad=json.dumps([session_id,iid,version,origin],separators=(',',':')).encode('utf-8')
    nonce=os.urandom(12)
    ciphertext=AESGCM(key).encrypt(nonce,plaintext,aad)
    del plaintext,key
    with get_conn() as conn:
        conn.execute('UPDATE research_sessions SET id=id WHERE id=?',(session_id,))
        current=_session(conn,session_id,writable=True)
        if _origin(current['url'])!=origin: raise HTTPException(409,'项目目标已变化，请重新确认并导入')
        if existing:
            row=_identity(conn,session_id,iid)
            if row['status']!=existing['status'] or row['version']!=existing['version']:
                raise HTTPException(409,'测试身份已变化，请刷新后重试')
        saved=conn.execute('SELECT fingerprint FROM research_identity_keys WHERE id=1').fetchone()
        if saved and saved[0]!=fingerprint: raise HTTPException(503,'身份密钥登记已变化，导入已停止')
        now=utcnow()
        conn.execute('INSERT OR IGNORE INTO research_identity_keys(id,fingerprint,created_at) VALUES(1,?,?)',(fingerprint,now))
        if existing:
            conn.execute("UPDATE research_identities SET status='active',kind=?,origin=?,version=?,ciphertext=?,nonce=?,updated_at=? WHERE session_id=? AND id=?",
                (kind,origin,version,ciphertext,nonce,now,session_id,iid))
        else:
            conn.execute("INSERT INTO research_identities(id,session_id,label,kind,origin,version,status,ciphertext,nonce,created_at,updated_at) VALUES(?,?,?,?,?,1,'active',?,?,?,?)",
                (iid,session_id,label.strip(),kind,origin,ciphertext,nonce,now,now))
        append_event(conn,session_id,'identity','测试身份已更新' if existing else '测试身份已保管',
            '身份材料已加密保存在 Kali，仅绑定当前目标；尚未用于登录或测试。',{'identity_id':iid,'version':version})
        return _metadata(_identity(conn,session_id,iid),current['url'])


def revoke_identity(session_id,identity_id):
    with get_conn() as conn:
        conn.execute('UPDATE research_sessions SET id=id WHERE id=?',(session_id,))
        session=_session(conn,session_id,writable=True)
        row=_identity(conn,session_id,identity_id)
        if row['status']!='revoked':
            conn.execute("UPDATE research_identities SET status='revoked',ciphertext=NULL,nonce=NULL,updated_at=? WHERE session_id=? AND id=?",(utcnow(),session_id,identity_id))
            append_event(conn,session_id,'identity','测试身份已撤销','身份密文已清除；保留身份元数据与历史事件。',{'identity_id':identity_id,'version':row['version']})
        return _metadata(_identity(conn,session_id,identity_id),session['url'])
