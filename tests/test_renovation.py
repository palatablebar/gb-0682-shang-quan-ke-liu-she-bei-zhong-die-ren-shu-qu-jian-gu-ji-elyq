"""改造期东门分段归属与正式人数区间的测试。"""
import base64
import io
import csv as csv_mod
from pathlib import Path

import pytest

from app.errors import DomainError
from app.renovation import engine, service, store
from app.renovation.partition import validate_entry, zone_plan, split_interval, combine_regions
from app.renovation.export_out import bundle

DATA = Path(__file__).resolve().parents[1] / 'sample_data'


@pytest.fixture
def reno_db(tmp_path, monkeypatch):
    monkeypatch.setenv('RENO_DB_PATH', str(tmp_path / 'renovation.db'))
    store.save_base(base64.b64encode((DATA / 'renovation_base.csv').read_bytes()).decode())
    confirmed = [
        validate_entry({'zone': 'A', 'part': 'A1', 'label': '北段', 'status': 'confirmed',
                        'effective_from': '2026-11-16T00:00:00+00:00', 'effective_to': '',
                        'source': '边界记录'}),
        validate_entry({'zone': 'A', 'part': 'A2', 'label': '南段', 'status': 'confirmed',
                        'effective_from': '2026-11-16T00:00:00+00:00', 'effective_to': '',
                        'source': '边界记录'}),
    ]
    for e in confirmed:
        store.upsert_part(e, '2026-11-16T00:00:00+00:00')
    return store


@pytest.fixture
def observations(reno_db):
    return service.load_observations(store.base_observation())['observations']


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'main.db'))
    monkeypatch.setenv('RENO_DB_PATH', str(tmp_path / 'renovation.db'))
    from app.main import create_app
    app = create_app()
    app.config['TESTING'] = True
    return app.test_client()


# ---- 裁决原则：不沿用整区、不相加、按扶梯口去重 ----

def test_split_unknown_overlap_gives_conservative_interval():
    low, high, overlap = split_interval({'A1': 70, 'A2': 60}, {}, 'A1', 'A2')
    assert (low, high, overlap) == (70, 130, None)


def test_split_observed_overlap_subtracts_escalator_once():
    low, high, overlap = split_interval({'A1': 72, 'A2': 58}, {'A1|A2': 20}, 'A1', 'A2')
    assert (low, high, overlap) == (110, 110, 20)


def test_overlap_exceeding_segment_is_constraint():
    with pytest.raises(DomainError) as e:
        split_interval({'A1': 10, 'A2': 8}, {'A1|A2': 9}, 'A1', 'A2')
    assert e.value.code == 'CONSTRAINT'


def test_combine_regions_is_conservative():
    assert combine_regions([(110, 110), (112, 112), (92, 92)]) == (112, 314)


def test_zone_plan_statuses(observations):
    bucket = '2026-11-16T01:00:00+00:00'
    entries = list(store.list_parts())
    assert zone_plan(entries, 'A', bucket) == ('SPLIT', ['A1', 'A2'])
    pending = [dict(e, status='pending') for e in entries]
    assert zone_plan(pending, 'A', bucket)[0] == 'PENDING'
    merged = [dict(e, status='merged') for e in entries]
    assert zone_plan(merged, 'A', bucket) == ('WHOLE', ['A'])


def test_pending_attribution_never_enters_official(observations):
    entries = [dict(e, status='pending') for e in store.list_parts()]
    s = engine.apply(observations, entries)
    # A 段全部待确认：三个桶仍各有一条来自 B、C 的正式区间，但 A 段观测不并入
    assert len(s['calculations']) == 3
    assert all(c['upper'] <= 114 + 94 for c in s['calculations'])  # 上界仅 B、C，不含 A 段
    assert len(s['pending']) == 6
    assert all(p['code'] == 'ATTRIBUTION_PENDING' and p['attribution']['plan'] == 'PENDING'
               for p in s['pending'])
    assert s['business']['official_only'] is True


def test_merged_back_restores_whole_zone_handling(observations):
    entries = [dict(e, status='merged') for e in store.list_parts()]
    s = engine.apply(observations, entries)
    # A1/A2 在并回后不再是有效分区，观测被隔离而不是悄悄相加
    assert {q['code'] for q in s['quarantine']} == {'UNMAPPED_PART'}
    assert all(c['lower'] >= 110 for c in s['calculations'])


def test_confirmed_without_boundary_uses_interval_not_forged_exact(observations):
    s = engine.apply(observations, service.freeze_entries())
    first = next(c for c in s['calculations'] if c['bucket'].endswith('T01:00:00+00:00'))
    # A 段 70/60 无 pair -> A 区间 [70,130]，与 B/C 联合后下界 110、上界 330
    assert (first['lower'], first['upper']) == (110, 330)
    assert first['code'] == 'INTERVAL'


# ---- 补传边界：仅重算受影响时段、幂等、事务发布 ----

def test_upload_is_idempotent_same_record(reno_db):
    raw = (DATA / 'renovation_boundary.csv').read_bytes()
    first = service.upload_boundary(raw)
    second = service.upload_boundary(raw)
    assert first['created'] is True and second['created'] is False
    assert len(store.staged_boundaries()) == 1


def test_partial_recompute_only_touches_affected_buckets(reno_db):
    snap1, _, digests1 = service.recompute()
    v1 = store.publish(snap1, [], digests1, 0, '2026-11-16T04:00:00+00:00')
    service.upload_boundary((DATA / 'renovation_boundary.csv').read_bytes())
    snap2, affected, digests2 = service.recompute()
    assert affected == ['2026-11-16T02:00:00+00:00', '2026-11-16T03:00:00+00:00']
    assert snap2['kept_buckets'] == ['2026-11-16T01:00:00+00:00']
    kept = next(c for c in snap2['calculations'] if c['bucket'].endswith('T01:00:00+00:00'))
    old = next(c for c in snap1['calculations'] if c['bucket'].endswith('T01:00:00+00:00'))
    assert kept == old
    fixed = {c['bucket'][11:16]: (c['lower'], c['upper']) for c in snap2['calculations']}
    assert fixed['02:00'] == (112, 314) and fixed['03:00'] == (115, 323)
    v2 = store.publish(snap2, affected, digests2, v1, '2026-11-16T05:00:00+00:00')
    assert v2 == v1 + 1


def test_rerun_same_record_creates_no_new_version(reno_db):
    snap, _, dg = service.recompute()
    store.publish(snap, [], dg, 0, '2026-11-16T04:00:00+00:00')
    raw = (DATA / 'renovation_boundary.csv').read_bytes()
    service.upload_boundary(raw)
    service.upload_boundary(raw)  # 幂等，不新增暂存
    snap2, affected, dg2 = service.recompute()
    before = store.latest_version()
    store.publish(snap2, affected, dg2, before, '2026-11-16T05:00:00+00:00')
    # 再重跑：无新增边界，发布结果摘要不变且版本号只增加一次
    snap3, affected3, dg3 = service.recompute()
    assert snap3['digest'] == snap2['digest']
    with store.cursor() as db:
        assert db.execute('SELECT COUNT(*) AS n FROM boundaries').fetchone()['n'] == 1


def test_stale_publish_keeps_old_version(reno_db):
    snap, _, dg = service.recompute()
    store.publish(snap, [], dg, 0, '2026-11-16T04:00:00+00:00')
    service.upload_boundary((DATA / 'renovation_boundary.csv').read_bytes())
    snap2, affected, dg2 = service.recompute()
    store.publish(snap2, affected, dg2, 1, '2026-11-16T05:00:00+00:00')
    with pytest.raises(DomainError) as e:
        store.publish(snap2, affected, dg2, 1, '2026-11-16T06:00:00+00:00')
    assert e.value.status == 409 and e.value.code == 'STALE'
    assert store.latest_version() == 2


def test_failed_publish_rolls_back_and_keeps_old(reno_db, monkeypatch):
    snap, _, dg = service.recompute()
    store.publish(snap, [], dg, 0, '2026-11-16T04:00:00+00:00')
    service.upload_boundary((DATA / 'renovation_boundary.csv').read_bytes())

    def boom(*a, **k):
        raise RuntimeError('disk full')

    # 用独立上下文隔离 mark_applied 的还原，避免 monkeypatch.undo 连带回收
    # RENO_DB_PATH，导致后续读取落到默认空库。
    with monkeypatch.context() as m:
        m.setattr(store, 'mark_applied', boom)
        with pytest.raises(RuntimeError):
            service.publish(1)
    assert store.latest_version() == 1  # 旧版保留
    # 边界暂存仍在（未被标记应用），故障消除后可重新发布为 V2
    assert store.staged_boundaries()[0]['applied_version'] is None
    fixed = service.publish(1)
    assert fixed['version'] == 2 and fixed['recomputed_buckets']
    assert store.latest_version() == 2


def test_boundary_referencing_unknown_part_rejected(reno_db):
    bad = ('id,bucket,version,kind,left,right,count,unit,note\n'
           'x1,2026-11-16T10:00:00+08:00,1,zone,A9,,5,人,未登记分区\n')
    with pytest.raises(DomainError) as e:
        service.upload_boundary(bad.encode())
    assert e.value.code == 'REFERENCE'


def test_partial_recompute_keeps_evidence_of_unaffected_buckets(reno_db):
    # 把 A1 置为待确认：01:00 桶产生待确认证据；边界只影响 02/03 桶
    for p in store.list_parts():
        if p['part'] == 'A1':
            store.upsert_part({**p, 'status': 'pending'}, '2026-11-16T01:00:00+00:00')
    snap1, _, dg1 = service.recompute()
    v1 = store.publish(snap1, [], dg1, 0, '2026-11-16T04:00:00+00:00')
    pending_before = {p['bucket'] for p in snap1['pending']}
    assert '2026-11-16T01:00:00+00:00' in pending_before
    service.upload_boundary((DATA / 'renovation_boundary.csv').read_bytes())
    snap2, _, _ = service.recompute()
    v2 = store.publish(snap2, ['2026-11-16T02:00:00+00:00'], dg1, v1,
                       '2026-11-16T05:00:00+00:00')
    published = store.get_version(v2)
    # 01:00 不在受影响桶，其待确认证据必须从旧快照保留
    assert any(p['bucket'] == '2026-11-16T01:00:00+00:00'
               for p in published['pending'])
    assert '2026-11-16T01:00:00+00:00' in published['kept_buckets']


def test_same_version_conflict_blocks_publish_and_keeps_old(reno_db):
    snap, _, dg = service.recompute()
    store.publish(snap, [], dg, 0, '2026-11-16T04:00:00+00:00')
    # 与基础观测同 (bucket,kind,left) 但同版不同值，合并期才暴露冲突
    bad = ('id,bucket,version,kind,left,right,count,unit,note\n'
           'z1,2026-11-16T09:00:00+08:00,1,zone,A1,,99,人,冲突同版\n')
    assert service.upload_boundary(bad.encode())['created'] is True
    with pytest.raises(DomainError) as e:
        service.publish(1)
    assert e.value.code == 'CONFLICT'
    assert store.latest_version() == 1  # 事务未开启，旧版完整保留


# ---- 页面与归档证据同一份分区归属 ----

def test_export_and_snapshot_share_attribution(reno_db):
    service.upload_boundary((DATA / 'renovation_boundary.csv').read_bytes())
    snap = service.publish(0)
    text = bundle(snap)
    rows = list(csv_mod.reader(io.StringIO(text.lstrip('﻿'))))
    official = [r for r in rows if r[0] == '正式区间']
    assert official and all(r[5] for r in official)  # 每条正式区间带冻结归属
    # 归属内容即快照内 attributions，页面与导出同源
    for r in official:
        bucket = r[2]
        expect = {a['part'] for a in snap['attributions'] if a['bucket'] == bucket}
        if expect:
            for part in expect:
                assert part in r[5]


def test_pending_rows_exported_separately(reno_db):
    for p in store.list_parts():
        store.upsert_part({**p, 'status': 'pending'}, '2026-11-16T01:00:00+00:00')
    snap = service.publish(0)
    text = bundle(snap)
    assert '待确认' in text and 'ATTRIBUTION_PENDING' in text
    # 待确认分区的原始观测不能出现在正式区间行
    assert all(p['code'] != 'EXACT' for p in snap['pending'])


# ---- HTTP 入口 ----

def test_http_full_flow(client):
    page = client.get('/renovation')
    assert page.status_code == 200 and '改造期' in page.data.decode()
    base_csv = (DATA / 'renovation_base.csv').read_text()
    assert client.post('/api/renovation/base', json={'csv': base_csv}).status_code == 200
    # 种子归属默认 pending，先确认
    for part in ('A1', 'A2'):
        r = client.post('/api/renovation/parts', json={
            'part': part, 'zone': 'A', 'status': 'confirmed',
            'effective_from': '2026-11-16T00:00:00+00:00', 'effective_to': '',
            'label': part, 'source': '测试登记'})
        assert r.status_code == 200
    # 发布 V1
    r = client.post('/api/renovation/publish', json={'expected_version': 0})
    assert r.status_code == 200 and r.get_json()['version'] == 1
    # 补传边界
    boundary = (DATA / 'renovation_boundary.csv').read_text()
    r = client.post('/api/renovation/boundaries', json={'csv': boundary})
    assert r.status_code == 200 and r.get_json()['created'] is True
    # 陈旧版本号发布 -> 409，旧版保留
    stale = client.post('/api/renovation/publish', json={'expected_version': 0})
    assert stale.status_code == 409
    ok = client.post('/api/renovation/publish', json={'expected_version': 1})
    assert ok.status_code == 200 and ok.get_json()['version'] == 2
    export = client.get('/api/renovation/versions/2/export.csv')
    assert export.status_code == 200 and '正式区间' in export.data.decode('utf-8-sig')


def test_http_publish_requires_version(client):
    r = client.post('/api/renovation/publish', json={})
    assert r.status_code == 422 and r.get_json()['code'] == 'VERSION'


def test_http_boundary_bad_csv_rejected(client):
    r = client.post('/api/renovation/boundaries', json={'csv': 'wrong\n1\n'})
    assert r.status_code == 422


def test_seed_default_plan_is_pending_and_idempotent(client):
    assert service.seed_default_plan() is True
    parts = store.list_parts()
    assert {p['part'] for p in parts} == {'A1', 'A2'}
    assert all(p['status'] == 'pending' for p in parts)
    assert service.seed_default_plan() is False
