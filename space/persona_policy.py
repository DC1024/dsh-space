"""人格权限的唯一真源（表 / 里）。

允许的范围只到本空间内的普通文件，不包含控制程序、权限规则、审批记录、
身份认证、预算账本、宿主文件或任何设备/网络范围的扩大。

- workdir/<人格>/：各自的作品与笔记，可读、可写、可列目录。
- dist/：界面文件（html/css/js/svg/png/jpg/ico/webp/md）。表可写，里只读。
- peer/：另一个人的工作区，只读。里用它对表的工作做核对；表也可读里。
- 表的每次写入（工作区或界面）都进入审查队列，供里事后核对。

界面写入只开放静态资产后缀，避免把执行器、配置或凭据写进 dist。
本模块不导入 space_tools，避免循环依赖；路径安全检查在这里实现并复用。
"""
import os
import stat
from pathlib import Path

PERSONAS = {'biao': '表', 'li': '里'}
PEER = {'biao': 'li', 'li': 'biao'}
INTERFACE_DIR = 'dist'
INTERFACE_SUFFIXES = frozenset({'.html', '.css', '.js', '.svg', '.png', '.jpg', '.ico', '.webp', '.md'})
REVIEW_DECISIONS = ('pass', 'flag', 'block')

# 读：scope -> 相对空间根的位置。写：只列可写 scope。
READ_SCOPES = {
    'biao': {'workspace': 'workdir/biao', 'interface': INTERFACE_DIR, 'peer': 'workdir/li'},
    'li': {'workspace': 'workdir/li', 'interface': INTERFACE_DIR, 'peer': 'workdir/biao'},
}
WRITE_SCOPES = {
    'biao': {'workspace': 'workdir/biao', 'interface': INTERFACE_DIR},
    'li': {'workspace': 'workdir/li'},
}


def label(actor):
    return PERSONAS.get(actor, str(actor))


def classify(raw):
    """把模型写的相对路径分成 工作区 / 界面 / 对方工作区 三个命名空间。"""
    text = raw.replace('\\', '/')
    if text.startswith('dist/'):
        return 'interface', text[5:]
    if text == 'dist':
        return 'interface', ''
    if text.startswith('peer/'):
        return 'peer', text[5:]
    if text == 'peer':
        return 'peer', ''
    return 'workspace', text


def reject_workspace_links(path):
    """父目录一起查：先解析联接再检查子路径会漏掉越界。"""
    path = Path(os.path.abspath(path))
    for node in [*reversed(path.parents), path]:
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError('工作区路径及父目录不得使用符号链接或目录联接')
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValueError('不允许多硬链接文件')


def resolve(store, raw='', write=False, create=False):
    """返回 (基准目录, 目标路径, 范围)；越界或越权时抛 ValueError。"""
    if not isinstance(raw, str):
        raise ValueError('只能使用本空间内的相对路径')
    if '\x00' in raw or raw.startswith(('/', '\\')) or (len(raw) > 1 and raw[1] == ':'):
        raise ValueError('只能使用本空间内的相对路径')
    actor = store.identity.actor()
    if actor not in PERSONAS:
        raise ValueError('人格必须为表或里')
    scope, rel = classify(raw)
    if write and scope == 'peer':
        raise ValueError('对方的工作区只有只读权限')
    if write and scope == 'interface' and actor != 'biao':
        raise ValueError('界面文件只允许表人格修改，里请用审查工具')
    roots = WRITE_SCOPES if write else READ_SCOPES
    relative = roots.get(actor, {}).get(scope)
    if not relative:
        raise ValueError('当前人格没有该范围的权限')
    base = Path(store.root) / relative
    parts = Path(rel).parts
    if any(part in ('..', '.versions') or part.rstrip(' .') != part for part in parts):
        raise ValueError('路径越界或保留目录')
    candidate = base.joinpath(*parts) if parts else base
    # 先做链接与越界检查，再创建目录：避免通过目录联接先建出工作区。
    reject_workspace_links(candidate)
    candidate.resolve().relative_to(base.resolve())
    base.mkdir(parents=True, exist_ok=True)
    if write and scope == 'interface' and candidate != base:
        if candidate.suffix.lower() not in INTERFACE_SUFFIXES:
            raise ValueError('界面目录只允许静态资产：' + ' '.join(sorted(INTERFACE_SUFFIXES)))
    if create and candidate != base:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    return base, candidate, scope


def describe():
    """给控制台和文档使用的简介；不含任何可用凭据。"""
    return {
        'biao': {'label': '表', 'write': ['workdir/biao', 'dist'], 'read': ['workdir/biao', 'dist', 'workdir/li'],
                 'review': 'writes_queued', 'tools': ['workspace_list', 'workspace_read', 'workspace_write']},
        'li': {'label': '里', 'write': ['workdir/li'], 'read': ['workdir/li', 'workdir/biao', 'dist'],
               'review': 'reviews_biao_writes',
               'tools': ['workspace_list', 'workspace_read', 'workspace_write',
                         'workspace_review_list', 'workspace_read_diff', 'workspace_review_decide']},
        'notes': ['审查是事后核对，不是事前批准；不恢复活动、不改暂停、不删除审查记录。',
                  '内人格未启用的现状保持不变；三人格合并视图暂不实现。'],
    }
