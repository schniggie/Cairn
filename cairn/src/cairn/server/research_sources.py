"""Bounded local text-file capture. Does not execute source or invoke external tools.

Snapshots identify the selected captured bytes, not an atomic repository state or
Git revision. GET operations read SQLite only and never revisit the repository.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import PurePosixPath
import stat
import time
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.research_services import append_event, record_evidence
from cairn.server.services import utcnow

LIMITS = {'max_file_bytes':512*1024, 'max_total_bytes':16*1024*1024,
          'max_files':2000, 'max_entries':10000, 'max_seconds':15,
          'max_depth':32, 'max_omissions':200}
SKIP_DIRECTORIES = {'node_modules','.venv','venv','vendor','build','dist','__pycache__'}
VERSION_NOTE = '版本摘要仅标识本次采集文件；整个目录采集不是原子的，也不是 Git commit。'
EXTENSIONS = {
    '.py':'python','.pyi':'python','.js':'javascript','.mjs':'javascript','.cjs':'javascript',
    '.jsx':'jsx','.ts':'typescript','.tsx':'tsx','.java':'java','.kt':'kotlin','.kts':'kotlin',
    '.php':'php','.go':'go','.rs':'rust','.rb':'ruby','.c':'c','.h':'c','.cpp':'cpp','.hpp':'cpp',
    '.cc':'cpp','.cs':'csharp','.swift':'swift','.scala':'scala','.sh':'shell','.bash':'shell',
    '.ps1':'powershell','.html':'html','.htm':'html','.css':'css','.scss':'scss',
    '.sql':'sql','.json':'json','.yaml':'yaml','.yml':'yaml','.toml':'toml','.xml':'xml',
    '.md':'markdown','.rst':'text','.txt':'text','.vue':'vue','.svelte':'svelte',
    '.lua':'lua','.pl':'perl','.ex':'elixir','.exs':'elixir','.erl':'erlang',
    '.dart':'dart','.r':'r','.R':'r','.gradle':'groovy','.groovy':'groovy',
}
NAMED_FILES = {'Dockerfile':'dockerfile','Makefile':'makefile','Gemfile':'ruby',
               'Rakefile':'ruby','CMakeLists.txt':'cmake','requirements.txt':'text',
               'Cargo.lock':'toml','go.mod':'go','go.sum':'text'}


def _json(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def language_for(path):
    name=PurePosixPath(path).name
    return NAMED_FILES.get(name,EXTENSIONS.get(PurePosixPath(name).suffix,'text'))


def _mount_id(fd):
    # Linux fdinfo distinguishes bind mounts even when st_dev is unchanged.
    with open('/proc/self/fdinfo/'+str(fd),'r',encoding='ascii') as stream:
        for line in stream:
            if line.startswith('mnt_id:'): return int(line.split(':',1)[1].strip())
    raise OSError('Mount identity unavailable')


def _open_root(repo):
    path=PurePosixPath(repo)
    if not path.is_absolute() or '..' in path.parts:
        raise HTTPException(422,'代码目录不是有效的绝对路径')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        # Resolve every ancestor without following symlinks, not just the final component.
        for name in path.parts[1:]:
            next_fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
            os.close(fd)
            fd=next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _session(conn,session_id,writable=False):
    row=conn.execute('SELECT id,repo,status,authorization_revision FROM research_sessions WHERE id=?',(session_id,)).fetchone()
    if row is None: raise HTTPException(404,'研究不存在')
    if writable:
        if row['status'] in ('running','pause_requested'):
            raise HTTPException(409,'请先暂停并等待执行器停止，再采集源码材料')
        if not row['repo']: raise HTTPException(409,'请先提供已授权的 Kali 本地代码目录')
    return row


def capture_directory(repo,limits=None):
    limits=dict(LIMITS if limits is None else limits)
    started=time.monotonic()
    files=[]
    omissions=[]
    limitations=[VERSION_NOTE]
    total=0
    visited=0
    stopped=False
    omitted=0
    visited_directories=set()

    def omit(path,reason,stop=False):
        nonlocal stopped,omitted
        omitted+=1
        safe_path=path.encode('utf-8',errors='backslashreplace').decode('utf-8')
        if len(omissions)<limits['max_omissions']:
            omissions.append({'path':safe_path,'reason':reason})
        elif '跳过详情达到显示上限，未逐项列出所有条目。' not in limitations:
            limitations.append('跳过详情达到显示上限，未逐项列出所有条目。')
        if stop:
            stopped=True
            if reason not in limitations: limitations.append(reason)

    def timed_out(path):
        if time.monotonic()-started>=limits['max_seconds']:
            omit(path,'已达到目录采集时间上限。',True)
        return stopped

    def attrs(info):
        return (info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

    try:
        root_fd=_open_root(repo)
    except (OSError,ValueError):
        raise HTTPException(422,'无法安全打开授权代码目录；请检查目录存在、权限与符号链接') from None
    try:
        root_stat=os.fstat(root_fd)
        try: root_mount=_mount_id(root_fd)
        except OSError:
            raise HTTPException(503,'无法确认代码目录的挂载边界，未开始采集') from None

        def within_boundary(fd):
            info=os.fstat(fd)
            return info.st_dev==root_stat.st_dev and _mount_id(fd)==root_mount

        def capture_file(directory_fd,name,path,before):
            nonlocal total
            fd=None
            try:
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
                initial=os.fstat(fd)
                if not stat.S_ISREG(initial.st_mode):
                    omit(path,'不是普通文件。'); return
                if initial.st_nlink!=1:
                    omit(path,'跳过硬链接文件。'); return
                if not within_boundary(fd):
                    omit(path,'跳过跨挂载边界的文件。'); return
                if attrs(before)!=attrs(initial):
                    omit(path,'文件在打开前发生变化。'); return
                if initial.st_size>limits['max_file_bytes']:
                    omit(path,'文件超过单文件大小上限。'); return
                if total+initial.st_size>limits['max_total_bytes']:
                    omit(path,'已达到源码内容总大小上限。',True); return
                chunks=[]
                length=0
                while length<=limits['max_file_bytes']:
                    if timed_out(path): return
                    chunk=os.read(fd,min(65536,limits['max_file_bytes']+1-length))
                    if not chunk: break
                    chunks.append(chunk)
                    length+=len(chunk)
                if attrs(initial)!=attrs(os.fstat(fd)) or attrs(initial)!=attrs(os.stat(name,dir_fd=directory_fd,follow_symlinks=False)):
                    omit(path,'文件在读取过程中发生变化。'); return
                if length>limits['max_file_bytes']:
                    omit(path,'文件超过单文件大小上限。'); return
                if total+length>limits['max_total_bytes']:
                    omit(path,'已达到源码内容总大小上限。',True); return
                raw=b''.join(chunks)
                try:
                    content=raw.decode('utf-8')
                    if '\x00' in content: raise UnicodeError()
                except UnicodeError:
                    omit(path,'不是可采集的 UTF-8 文本。'); return
                files.append({'path':path,'content':content,'sha256':hashlib.sha256(raw).hexdigest(),
                              'bytes':length,'lines':len(content.splitlines()),'language':language_for(path)})
                total+=length
            except OSError:
                omit(path,'文件无法安全读取或已变化。')
            finally:
                if fd is not None: os.close(fd)

        def walk(directory_fd,prefix,depth):
            nonlocal visited
            if timed_out(prefix or '.'): return
            if depth>limits['max_depth']:
                omit(prefix,'目录深度超过采集上限。'); return
            info=os.fstat(directory_fd)
            marker=(info.st_dev,info.st_ino)
            if marker in visited_directories:
                omit(prefix,'目录重复或形成循环。'); return
            visited_directories.add(marker)
            names=[]
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        if timed_out(prefix or '.'): break
                        if visited>=limits['max_entries']:
                            omit(prefix or '.','已达到目录条目数量上限。');
                            if '已达到目录条目数量上限。' not in limitations: limitations.append('已达到目录条目数量上限。')
                            break
                        visited+=1
                        names.append(entry.name)
            except OSError:
                omit(prefix or '.','目录无法读取。')
            # Sorting is bounded by max_entries, never an unbounded directory list.
            for name in sorted(names):
                if stopped or timed_out(prefix or '.'): break
                path=prefix+'/'+name if prefix else name
                try: path.encode('utf-8')
                except UnicodeError:
                    omit(path,'跳过非 UTF-8 文件名。'); continue
                if len(path)>4096:
                    omit(path,'文件路径超过长度上限。'); continue
                if name.startswith('.'):
                    omit(path,'默认跳过隐藏条目。'); continue
                try: entry_stat=os.stat(name,dir_fd=directory_fd,follow_symlinks=False)
                except OSError:
                    omit(path,'条目无法读取或已移除。'); continue
                if stat.S_ISLNK(entry_stat.st_mode):
                    omit(path,'跳过符号链接。'); continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    if name in SKIP_DIRECTORIES:
                        omit(path,'默认跳过依赖或构建目录。'); continue
                    child=None
                    try:
                        child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
                        opened=os.fstat(child)
                        if (opened.st_dev,opened.st_ino)!=(entry_stat.st_dev,entry_stat.st_ino):
                            omit(path,'目录在打开前发生变化。'); continue
                        if not within_boundary(child):
                            omit(path,'跳过跨挂载边界的目录。'); continue
                        walk(child,path,depth+1)
                    except OSError:
                        omit(path,'目录无法安全打开。')
                    finally:
                        if child is not None: os.close(child)
                elif stat.S_ISREG(entry_stat.st_mode):
                    if len(files)>=limits['max_files']:
                        omit(path,'已达到采集文件数量上限。',True); break
                    capture_file(directory_fd,name,path,entry_stat)
                else:
                    omit(path,'跳过特殊文件。')

        walk(root_fd,'',0)
    finally:
        os.close(root_fd)
    files.sort(key=lambda item:item['path'])
    digest=hashlib.sha256(_json([[item['path'],item['sha256']] for item in files]).encode('utf-8')).hexdigest()
    if omitted: limitations.append('存在未采集条目，请结合跳过原因理解本次材料范围。')
    return {'files':files,'omissions':omissions,'limits':limits,'limitations':limitations,
            'digest':digest,'total_bytes':total,'file_count':len(files),
            'languages':sorted({item['language'] for item in files}),
            'status':'partial' if omitted else 'complete'}


def _summary(row,current_repo):
    keys=('id','repo','created_at','digest','file_count','total_bytes','status')
    result={key:row[key] for key in keys}
    result['languages']=json.loads(row['languages_json'])
    result['limitations']=json.loads(row['limitations_json'])
    result['stale']=row['repo']!=current_repo
    return result


def _snapshot(conn,session_id,snapshot_id):
    row=conn.execute('SELECT * FROM research_source_snapshots WHERE session_id=? AND id=?',(session_id,snapshot_id)).fetchone()
    if row is None: raise HTTPException(404,'源码快照不存在')
    return row


def list_snapshots(session_id):
    with get_conn() as conn:
        conn.execute('BEGIN')
        session=_session(conn,session_id)
        rows=conn.execute('SELECT * FROM research_source_snapshots WHERE session_id=? ORDER BY created_at DESC,rowid DESC',(session_id,))
        return {'items':[_summary(row,session['repo']) for row in rows]}


def _detail(conn,session_id,snapshot_id):
    session=_session(conn,session_id)
    row=_snapshot(conn,session_id,snapshot_id)
    result=_summary(row,session['repo'])
    result['limits']=json.loads(row['limits_json'])
    result['omissions']=json.loads(row['omissions_json'])
    result['files']=[dict(item) for item in conn.execute('SELECT path,sha256,bytes,lines,language FROM research_source_files WHERE snapshot_id=? ORDER BY path',(snapshot_id,))]
    return result


def get_snapshot(session_id,snapshot_id):
    with get_conn() as conn:
        conn.execute('BEGIN')
        return _detail(conn,session_id,snapshot_id)


def get_file(session_id,snapshot_id,path):
    with get_conn() as conn:
        conn.execute('BEGIN')
        _snapshot(conn,session_id,snapshot_id)
        row=conn.execute('SELECT path,content,sha256,language,snapshot_id FROM research_source_files WHERE snapshot_id=? AND path=?',(snapshot_id,path)).fetchone()
        if row is None: raise HTTPException(404,'快照中没有该文件')
        return {**dict(row),'line_start':1}


def create_snapshot(session_id):
    with get_conn() as conn:
        session=dict(_session(conn,session_id,writable=True))
    captured=capture_directory(session['repo'])
    snapshot_id='source-'+uuid4().hex
    with get_conn() as conn:
        conn.execute('UPDATE research_sessions SET id=id WHERE id=?',(session_id,))
        current=_session(conn,session_id,writable=True)
        if current['repo']!=session['repo'] or current['authorization_revision']!=session['authorization_revision']:
            raise HTTPException(409,'采集期间项目材料或授权发生变化，请重新采集')
        conn.execute('INSERT INTO research_source_snapshots (id,session_id,repo,authorization_revision,created_at,digest,file_count,total_bytes,languages_json,status,limitations_json,omissions_json,limits_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (snapshot_id,session_id,session['repo'],session['authorization_revision'],utcnow(),captured['digest'],captured['file_count'],captured['total_bytes'],_json(captured['languages']),captured['status'],_json(captured['limitations']),_json(captured['omissions']),_json(captured['limits'])))
        conn.executemany('INSERT INTO research_source_files (snapshot_id,path,content,sha256,bytes,lines,language) VALUES (?,?,?,?,?,?,?)',
            [(snapshot_id,item['path'],item['content'],item['sha256'],item['bytes'],item['lines'],item['language']) for item in captured['files']])
        append_event(conn,session_id,'source','源码材料版本已保存',
            '已保存 '+str(captured['file_count'])+' 个文本文件。'+VERSION_NOTE,
            {'snapshot_id':snapshot_id,'digest':captured['digest'],'file_count':captured['file_count'],'status':captured['status']})
        return _detail(conn,session_id,snapshot_id)


def preserve_lines(session_id,snapshot_id,path,start_line,end_line):
    if (type(start_line) is not int or type(end_line) is not int or start_line<1
        or end_line<start_line or end_line-start_line+1>400):
        raise HTTPException(422,'请选择有效的起止行，单次最多保留 400 行')
    with get_conn() as conn:
        conn.execute('UPDATE research_sessions SET id=id WHERE id=?',(session_id,))
        session=_session(conn,session_id)
        if session['status'] in ('running','pause_requested'):
            raise HTTPException(409,'请先暂停并等待执行器停止，再保留人工材料引用')
        snapshot=_snapshot(conn,session_id,snapshot_id)
        file=conn.execute('SELECT * FROM research_source_files WHERE snapshot_id=? AND path=?',(snapshot_id,path)).fetchone()
        if file is None: raise HTTPException(404,'快照中没有该文件')
        lines=file['content'].splitlines(keepends=True)
        if end_line>len(lines): raise HTTPException(422,'所选行超出已保存文件范围')
        content=''.join(lines[start_line-1:end_line])
        metadata={'path':path,'line_start':start_line,'line_end':end_line,
                  'snapshot_id':snapshot_id,'snapshot_digest':snapshot['digest'],
                  'sha256':file['sha256'],'fragment_sha256':hashlib.sha256(content.encode('utf-8')).hexdigest(),
                  'language':file['language'],'repo':snapshot['repo'],
                  'version':snapshot['digest'],'version_kind':'captured_files_digest',
                  'limitation':VERSION_NOTE}
        evidence=record_evidence(conn,session_id,'code',path+':'+str(start_line)+'–'+str(end_line),content,metadata)
        append_event(conn,session_id,'evidence','已保留源码材料引用',
            '已从保存的文件版本选择代码行；这不是漏洞结论或验证结果。',
            {'snapshot_id':snapshot_id,'snapshot_digest':snapshot['digest'],'path':path,'start_line':start_line,'end_line':end_line},
            evidence_ids=[evidence['id']])
        return {'evidence_id':evidence['id']}
