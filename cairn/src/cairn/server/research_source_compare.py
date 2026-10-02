"""Read-only comparisons of captured text. No filesystem reads or source execution."""
from __future__ import annotations

import difflib

from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.research_sources import _session, _snapshot, _summary

MAX_SIDE_BYTES = 64 * 1024
MAX_SIDE_LINES = 1200
MAX_LINE_CHARACTERS = 8192
MAX_OUTPUT_LINES = 1600
CONTEXT_LINES = 3

BASE_LIMITATIONS = [
    '出现和缺失仅表示文件是否被两个快照采集，不能据此断言磁盘文件新建或删除。',
    '对照只比较保存的相对路径和内容，不推断重命名、漏洞结论或修复成功。',
    '版本摘要标识采集文件内容，不是原子仓库状态或 Git commit。',
]


def _read_context(conn,session_id,before_id,after_id):
    if not conn.in_transaction: conn.execute('BEGIN')
    session=_session(conn,session_id)
    before=_snapshot(conn,session_id,before_id)
    after=_snapshot(conn,session_id,after_id)
    limitations=list(BASE_LIMITATIONS)
    if before['status']=='partial' or after['status']=='partial':
        limitations.append('至少一个快照仅采集部分材料；缺失文件也可能是跳过、读取失败或采集限制所致。')
    if before['repo']!=after['repo']:
        limitations.append('两个快照来自不同代码目录，仅按相对路径对照；同名文件不保证具有相同用途或来源。')
    return _summary(before,session['repo']),_summary(after,session['repo']),limitations


def _change(before,after):
    if before is None: return 'added'
    if after is None: return 'removed'
    return 'unchanged' if before['sha256']==after['sha256'] else 'modified'


def _file_meta(path,before,after):
    return {'path':path,'change':_change(before,after),
            'before_sha256':before['sha256'] if before is not None else None,
            'after_sha256':after['sha256'] if after is not None else None}


def compare_snapshots(session_id,before_id,after_id):
    with get_conn() as conn:
        conn.execute('PRAGMA query_only=ON')
        before,after,limitations=_read_context(conn,session_id,before_id,after_id)
        before_files={row['path']:row for row in conn.execute('SELECT path,sha256 FROM research_source_files WHERE snapshot_id=?',(before_id,))}
        after_files={row['path']:row for row in conn.execute('SELECT path,sha256 FROM research_source_files WHERE snapshot_id=?',(after_id,))}
        counts={'added':0,'removed':0,'modified':0,'unchanged':0}
        files=[]
        for path in sorted(before_files.keys()|after_files.keys()):
            item=_file_meta(path,before_files.get(path),after_files.get(path))
            counts[item['change']]+=1
            if item['change']!='unchanged': files.append(item)
        return {'before':before,'after':after,'counts':counts,'files':files,'limitations':limitations}


def _line_parts(line):
    if line.endswith('\r\n'): return line[:-2],'crlf'
    if line.endswith('\n'): return line[:-1],'lf'
    if line.endswith('\r'): return line[:-1],'cr'
    if line and line[-1] in ('\v','\f','\x1c','\x1d','\x1e','\x85','\u2028','\u2029'):
        return line[:-1],'other'
    return line,'none'


def _range(start,stop):
    """Unified diff ranges use zero for an empty insertion/deletion anchor."""
    length=stop-start
    if length==1: return str(start+1)
    return str(start if length==0 else start+1)+','+str(length)


def _diff_lines(before,after):
    result=[]
    truncated=False
    def emit(kind,before_line,after_line,text,eol=None):
        nonlocal truncated
        if len(result)>=MAX_OUTPUT_LINES:
            truncated=True
            return False
        result.append({'kind':kind,'before_line':before_line,'after_line':after_line,'text':text,'eol':eol})
        return True
    matcher=difflib.SequenceMatcher(None,before,after,autojunk=True)
    for group in matcher.get_grouped_opcodes(CONTEXT_LINES):
        first,last=group[0],group[-1]
        if not emit('hunk',None,None,'@@ -'+_range(first[1],last[2])+' +'+_range(first[3],last[4])+' @@'): break
        for tag,i1,i2,j1,j2 in group:
            if tag=='equal':
                for i,j in zip(range(i1,i2),range(j1,j2)):
                    text,eol=_line_parts(before[i])
                    if not emit('context',i+1,j+1,text,eol): break
            if tag in ('replace','delete'):
                for i in range(i1,i2):
                    text,eol=_line_parts(before[i])
                    if not emit('removed',i+1,None,text,eol): break
            if tag in ('replace','insert') and not truncated:
                for j in range(j1,j2):
                    text,eol=_line_parts(after[j])
                    if not emit('added',None,j+1,text,eol): break
            if truncated: break
        if truncated: break
    return result,truncated


def compare_file(session_id,before_id,after_id,path):
    with get_conn() as conn:
        conn.execute('PRAGMA query_only=ON')
        _,_,limitations=_read_context(conn,session_id,before_id,after_id)
        before=conn.execute('SELECT path,sha256,bytes,lines FROM research_source_files WHERE snapshot_id=? AND path=?',(before_id,path)).fetchone()
        after=conn.execute('SELECT path,sha256,bytes,lines FROM research_source_files WHERE snapshot_id=? AND path=?',(after_id,path)).fetchone()
        if before is None and after is None: raise HTTPException(404,'两个快照中都没有该文件')
        result={**_file_meta(path,before,after),'lines':[],'truncated':False,'limitations':limitations}
        if result['change']=='unchanged':
            limitations.append('两个快照记录的文件字节摘要相同，没有内容差异。')
            return result
        if any(row is not None and (row['bytes']>MAX_SIDE_BYTES or row['lines']>MAX_SIDE_LINES) for row in (before,after)):
            result['truncated']=True
            limitations.append('文件超过逐行对照上限（每侧 64 KiB 或 1200 行），未计算行差异；请分别查看已保存原文件。')
            return result
        # Only load content after the recorded byte/line limits have passed.
        before_content=conn.execute('SELECT content FROM research_source_files WHERE snapshot_id=? AND path=?',(before_id,path)).fetchone()[0] if before is not None else ''
        after_content=conn.execute('SELECT content FROM research_source_files WHERE snapshot_id=? AND path=?',(after_id,path)).fetchone()[0] if after is not None else ''
    before_lines=before_content.splitlines(keepends=True)
    after_lines=after_content.splitlines(keepends=True)
    # Recheck actual stored text as well as index metadata, before diff matching.
    if (len(before_content.encode('utf-8'))>MAX_SIDE_BYTES or len(after_content.encode('utf-8'))>MAX_SIDE_BYTES
        or max(len(before_lines),len(after_lines))>MAX_SIDE_LINES
        or any(len(line)>MAX_LINE_CHARACTERS for line in before_lines+after_lines)):
        result['truncated']=True
        limitations.append('保存内容超过逐行对照的字节、行数或单行长度限制，未计算行差异；请查看原文件。')
        return result
    before_endings={_line_parts(line)[1] for line in before_lines}
    after_endings={_line_parts(line)[1] for line in after_lines}
    if before_endings!=after_endings:
        limitations.append('两侧的换行方式或末行终止符不同；行尾标记区分 LF、CRLF、CR、无终止符或其他终止符。')
    if (before_content!=after_content and [_line_parts(line)[0] for line in before_lines]==[_line_parts(line)[0] for line in after_lines]):
        limitations.append('可见行文本相同，差异仅在换行或行终止符；原始字节仍然不同。')
    if (before is not None and not before_content) or (after is not None and not after_content):
        limitations.append('至少一侧快照保存的是空文件；空文件与该路径未被快照采集是不同状态。')
    result['lines'],result['truncated']=_diff_lines(before_lines,after_lines)
    if result['truncated']:
        limitations.append('行差异超过 1600 行显示上限，剩余差异未展示；省略部分不能视为没有变化。')
    return result
