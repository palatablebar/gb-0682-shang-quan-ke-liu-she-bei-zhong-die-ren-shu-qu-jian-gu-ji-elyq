"""改造期服务：边界补传、受影响时段重算与事务发布。

边界记录复用主管线 m1-m3 的采信、映射、分桶与同版去重，保证编码、
时区与版本口径一致；随后用改造期裁决引擎（renovation.engine）按
冻结的分区归属重算。补传只重算受影响小时桶，未触及时段沿用旧快照。
"""
import base64
import hashlib
from datetime import datetime, timezone

from app.errors import DomainError
from app.modules.m1_ingest.reader import read
from app.modules.m2_mapping.rules import normalize
from app.modules.m3_versions.rules import apply as bucketize
from app.renovation import partition, engine, store


def _now():
    return datetime.now(timezone.utc).isoformat()


def _prepare(content):
    """复用 m1->m2->m3，输出标准观测（含 _line/_raw 来源信息）。"""
    return bucketize(normalize(read(content)))


def load_observations(b64):
    if not b64:
        raise DomainError('NOT_FOUND', '尚未载入改造期基础观测', 404)
    return _prepare(base64.b64decode(b64))


def freeze_entries():
    """从归属表读取当前归属并校验，作为本次快照冻结口径。"""
    rows = store.list_parts()
    return [partition.validate_entry({
        'zone': r['zone'], 'part': r['part'], 'label': r['label'], 'status': r['status'],
        'effective_from': r['effective_from'], 'effective_to': r['effective_to'],
        'source': r['source']}) for r in rows]


def _boundary_digest(rows):
    material = repr(sorted((r['id'], r['bucket'], r['kind'], r['left'], r['right'],
                            r['count'], str(r.get('version', 1))) for r in rows))
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def upload_boundary(content):
    """补传边界记录：解析暂存、按内容哈希幂等，返回受影响桶与是否新增。

    重跑同一记录（内容哈希相同）created=False，不新增暂存、不产生新版本。
    """
    prepared = _prepare(content)
    if prepared['quarantine']:
        codes = sorted({q['code'] for q in prepared['quarantine']})
        raise DomainError('BOUNDARY_INVALID', '边界记录含隔离数据：' + ','.join(codes))
    observations = prepared['observations']
    if not observations:
        raise DomainError('BOUNDARY_INVALID', '边界记录无有效观测')
    registered = {p['part'] for p in store.list_parts()}
    for r in observations:
        if r['kind'] == 'zone' and r['left'] not in registered:
            raise DomainError('REFERENCE', '边界记录引用了未登记分区：%s' % r['left'])
        if r['kind'] == 'pair' and (r['left'] not in registered or r['right'] not in registered):
            raise DomainError('REFERENCE', '边界重叠记录的分区未登记')
    buckets = sorted({r['bucket'] for r in observations})
    payload = [{'id': r['id'], 'bucket': r['bucket'], 'kind': r['kind'],
                'left': r['left'], 'right': r['right'], 'count': int(r['count']),
                'version': int(r.get('version', 1)), 'line': r.get('_line', 0),
                'raw': r.get('_raw', {})} for r in observations]
    digest = _boundary_digest(observations)
    created = store.stage_boundary(digest, buckets[0], payload, _now())
    return {'digest': digest, 'created': created, 'affected_buckets': buckets,
            'staged_count': len(store.staged_boundaries())}


def _merge_observations(base_obs, staged_payloads):
    """基础观测与暂存边界合并；同 (bucket,kind,left,right) 由高 version 胜出。

    直接复用 m3 的分组选版逻辑，同版冲突按既有 CONFLICT 规则隔离并拒绝发布。
    """
    rows = []
    for r in base_obs:
        rows.append(dict(r))
    for payload in staged_payloads:
        for r in payload:
            rows.append({'id': r['id'], 'bucket': r['bucket'], 'kind': r['kind'],
                         'left': r['left'], 'right': r['right'], 'count': r['count'],
                         'unit': '人', 'version': r.get('version', 1),
                         '_line': r.get('line', 0), '_raw': r.get('raw', {})})
    picked = bucketize({'rows': rows, 'quarantine': []})
    if picked['quarantine']:
        codes = sorted({q['code'] for q in picked['quarantine']})
        raise DomainError('CONFLICT', '边界与基础观测存在同版冲突：' + ','.join(codes))
    return picked['observations']


def recompute(affected_only=True):
    """重算正式快照。affected_only 时只替换受影响桶，其余沿用上个版本。"""
    entries = freeze_entries()
    base = load_observations(store.base_observation())
    staged = store.staged_boundaries()
    observations = _merge_observations(base['observations'], [s['payload'] for s in staged])
    fresh = engine.apply(observations, entries)

    affected = set()
    for s in staged:
        affected.update(r['bucket'] for r in s['payload'])

    previous = store.get_version() if store.latest_version() and affected_only else None
    fresh_by_bucket = {r['bucket']: r for r in fresh['calculations']}
    if previous:
        kept = [r for r in previous['calculations'] if r['bucket'] not in affected]
        merged = kept + [r for b, r in fresh_by_bucket.items() if b in affected]
        kept_buckets = [r['bucket'] for r in kept]
        # 非区间证据同样按桶合并：未触及时段沿用旧快照，受影响桶以本次重算为准。
        def kept_evidence(key):
            return [r for r in previous.get(key, []) if r.get('bucket') not in affected]
        pending = kept_evidence('pending') + [r for r in fresh['pending'] if r['bucket'] in affected]
        attributions = kept_evidence('attributions') + \
            [r for r in fresh['attributions'] if r['bucket'] in affected]
        quarantine = kept_evidence('quarantine') + \
            [r for r in fresh['quarantine'] if r['bucket'] in affected]
    else:
        merged = fresh['calculations']
        kept_buckets = []
        pending, attributions, quarantine = fresh['pending'], fresh['attributions'], fresh['quarantine']
    merged.sort(key=lambda r: r['bucket'])

    snapshot = {'entries': entries, 'calculations': merged,
                'pending': pending, 'attributions': attributions,
                'quarantine': quarantine, 'kept_buckets': kept_buckets,
                'recomputed_buckets': sorted(affected),
                'business': {**fresh['business'],
                             'total': sum(r['lower'] for r in merged),
                             'upper_total': sum(r['upper'] for r in merged),
                             'buckets': len(merged)}}
    snapshot['digest'] = hashlib.sha256(
        (repr(merged) + repr(entries)).encode()).hexdigest()[:20]
    return snapshot, sorted(affected), [s['digest'] for s in staged]


def publish(expected_version, affected_only=True):
    snapshot, affected, digests = recompute(affected_only=affected_only)
    version = store.publish(snapshot, affected, digests, expected_version, _now())
    return store.get_version(version)


def seed_default_plan():
    """首次使用时给出改造期东门两段的待确认归属模板，不自动确认。"""
    if store.list_parts():
        return False
    now = _now()
    store.upsert_part(partition.validate_entry(
        {'zone': 'A', 'part': 'A1', 'label': '东门北段（含扶梯口）', 'status': 'pending',
         'effective_from': '2026-11-16T00:00:00+00:00', 'effective_to': '',
         'source': '改造分区边界记录（待确认）'}), now)
    store.upsert_part(partition.validate_entry(
        {'zone': 'A', 'part': 'A2', 'label': '东门南段（含扶梯口）', 'status': 'pending',
         'effective_from': '2026-11-16T00:00:00+00:00', 'effective_to': '',
         'source': '改造分区边界记录（待确认）'}), now)
    return True
