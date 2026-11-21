"""改造期分区归属、边界记录与正式版本的持久化。

与主管线 runs 表分库语义：这里只保存“归属 + 边界 + 已发布正式区间快照”。
- 发布采用单一显式事务：先在事务内重算并写新版本，任何一步失败整体回滚，旧版保留；
- 条件更新版本号，陈旧发布（expected_version 不匹配）返回 409；
- 同一边界记录按内容哈希去重，重跑不新增版本、不重复暂存；
- 所有短连接显式关闭，读取始终拿到干净连接，避免悬挂事务挡住旧版可见性。
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from app.errors import DomainError


def _db_path():
    default = Path(__file__).resolve().parents[2] / 'data' / 'renovation.db'
    return Path(os.environ.get('RENO_DB_PATH', str(default)))


def connection():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # autocommit（isolation_level=None）：事务边界完全由 publish 的
    # BEGIN/COMMIT/ROLLBACK 显式控制，短查询不会意外开启隐式事务。
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('''CREATE TABLE IF NOT EXISTS zone_parts(
        part TEXT PRIMARY KEY, zone TEXT NOT NULL, label TEXT NOT NULL,
        status TEXT NOT NULL, effective_from TEXT, effective_to TEXT, source TEXT NOT NULL,
        updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS boundaries(
        digest TEXT PRIMARY KEY, bucket TEXT NOT NULL, payload TEXT NOT NULL,
        received_at TEXT NOT NULL, applied_version INTEGER)''')
    db.execute('''CREATE TABLE IF NOT EXISTS versions(
        version INTEGER PRIMARY KEY AUTOINCREMENT, snapshot TEXT NOT NULL,
        affected_buckets TEXT NOT NULL, created_at TEXT NOT NULL,
        boundary_digests TEXT NOT NULL)''')
    db.execute('CREATE TABLE IF NOT EXISTS meta_kv(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    return db


@contextmanager
def cursor():
    db = connection()
    try:
        yield db
    finally:
        db.close()


def list_parts():
    with cursor() as db:
        rows = db.execute('SELECT * FROM zone_parts ORDER BY part').fetchall()
    return [dict(r) for r in rows]


def upsert_part(entry, now):
    with cursor() as db:
        db.execute('''INSERT INTO zone_parts(part,zone,label,status,effective_from,effective_to,source,updated_at)
            VALUES(:part,:zone,:label,:status,:effective_from,:effective_to,:source,:updated_at)
            ON CONFLICT(part) DO UPDATE SET zone=excluded.zone,label=excluded.label,
            status=excluded.status,effective_from=excluded.effective_from,
            effective_to=excluded.effective_to,source=excluded.source,updated_at=excluded.updated_at''',
                   {**entry, 'updated_at': now})
    return next(p for p in list_parts() if p['part'] == entry['part'])


def get_part(part):
    with cursor() as db:
        r = db.execute('SELECT * FROM zone_parts WHERE part=?', (part,)).fetchone()
    if not r:
        raise DomainError('NOT_FOUND', '分区归属不存在', 404)
    return dict(r)


def staged_boundaries():
    with cursor() as db:
        rows = db.execute('SELECT digest,bucket,payload,received_at,applied_version FROM boundaries '
                          'ORDER BY received_at').fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item['payload'] = json.loads(item['payload'])
        out.append(item)
    return out


def stage_boundary(digest, bucket, payload, now):
    """按内容哈希幂等暂存：重复补传返回 False，不新增记录、不新增版本。"""
    with cursor() as db:
        cur = db.execute('INSERT OR IGNORE INTO boundaries(digest,bucket,payload,received_at,applied_version) '
                         'VALUES(?,?,?,?,NULL)', (digest, bucket, json.dumps(payload, ensure_ascii=False), now))
        return cur.rowcount == 1


def mark_applied(digests, version, db):
    db.execute('UPDATE boundaries SET applied_version=? WHERE digest IN (%s)'
               % ','.join('?' * len(digests)), [version, *digests])


def latest_version():
    with cursor() as db:
        r = db.execute('SELECT version FROM versions ORDER BY version DESC LIMIT 1').fetchone()
    return r['version'] if r else 0


def get_version(version=None):
    with cursor() as db:
        if version is None:
            r = db.execute('SELECT * FROM versions ORDER BY version DESC LIMIT 1').fetchone()
        else:
            r = db.execute('SELECT * FROM versions WHERE version=?', (version,)).fetchone()
    if not r:
        raise DomainError('NOT_FOUND', '正式版本不存在', 404)
    snap = json.loads(r['snapshot'])
    snap.update(version=r['version'],
                affected_buckets=json.loads(r['affected_buckets']),
                boundary_digests=json.loads(r['boundary_digests']),
                created_at=r['created_at'])
    return snap


def publish(snapshot, affected_buckets, digests, expected_version, now):
    """事务化条件发布。expected_version 不匹配抛 409；任何异常回滚，旧版保留。

    autocommit 连接上显式 BEGIN 开启单一顶层事务，ROLLBACK 只撤销本事务
    内的新版本写入；已提交的历史版本在另一连接中始终可见。
    """
    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT COALESCE(MAX(version),0) AS v FROM versions').fetchone()['v']
        if current != expected_version:
            raise DomainError('STALE', '正式版本已更新，请刷新后重跑', 409)
        body = json.dumps(snapshot, ensure_ascii=False, default=str)
        cur = db.execute('INSERT INTO versions(snapshot,affected_buckets,created_at,boundary_digests) '
                         'VALUES(?,?,?,?)',
                         (body, json.dumps(sorted(affected_buckets)), now, json.dumps(sorted(digests))))
        new_version = cur.lastrowid
        if digests:
            mark_applied(digests, new_version, db)
        db.commit()
        return new_version
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def base_observation():
    with cursor() as db:
        r = db.execute("SELECT value FROM meta_kv WHERE key='base_b64'").fetchone()
    return r['value'] if r else None


def save_base(b64):
    with cursor() as db:
        db.execute("INSERT INTO meta_kv(key,value) VALUES('base_b64',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (b64,))
