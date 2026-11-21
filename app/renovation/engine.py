"""改造期按小时桶裁决正式人数区间。

输入是已完成采信、映射、分桶与同版去重的观测（observations），
以及一份在发布快照中冻结的分区归属（entries）。
输出的每一条正式区间、待确认证据与隔离记录都带同一份 attribution，
区间页面与归档证据直接消费同一快照，避免两处口径分叉。
"""
from collections import defaultdict
from app.errors import DomainError
from app.modules.m5_calculate.rules import bounds
from app.renovation.partition import zone_plan, split_interval, combine_regions


def _group(bucket_rows):
    zones = {r['left']: r['count'] for r in bucket_rows if r['kind'] == 'zone'}
    pairs = {'|'.join(sorted([r['left'], r['right']])): r['count']
             for r in bucket_rows if r['kind'] == 'pair'}
    return zones, pairs


def _attribution(entries, zone, bucket, plan, parts):
    active = [e for e in entries if e['zone'] == zone and e['part'] in parts]
    return {'zone': zone, 'bucket': bucket, 'plan': plan, 'parts': parts,
            'statuses': {e['part']: e['status'] for e in active},
            'sources': sorted({e['source'] for e in active if e.get('source')})}


def _rows_for(rows, tokens):
    tokens = set(tokens)
    return [r for r in rows
            if r['left'] in tokens
            or (r['kind'] == 'pair' and (r['left'] in tokens or r['right'] in tokens))]


def rule_bucket(bucket, rows, entries):
    """裁决单个小时桶，返回 (正式区间或None, 待确认列表, [(code, rows)隔离])。"""
    zones, pairs = _group(rows)
    pending, quarantined, attributions = [], [], []
    region_intervals = []
    whole_zones = []
    claimed = set()

    for zone in ('A', 'B', 'C'):
        plan, parts = zone_plan(entries, zone, bucket)
        claimed.update(parts)
        if plan == 'PENDING':
            for r in _rows_for(rows, parts):
                pending.append({'line': r.get('_line', 0), 'id': r.get('id', ''),
                                'bucket': bucket, 'zone': zone, 'parts': parts,
                                'code': 'ATTRIBUTION_PENDING', 'raw': r.get('_raw', {}),
                                'attribution': _attribution(entries, zone, bucket, plan, parts)})
            continue
        if plan == 'SPLIT':
            try:
                low, high, overlap = split_interval(zones, pairs, parts[0], parts[1])
            except DomainError as exc:
                quarantined.append((exc.code, _rows_for(rows, parts)))
                continue
            region_intervals.append((low, high))
            for part in parts:
                attributions.append({'zone': zone, 'part': part, 'bucket': bucket,
                                     'plan': 'SPLIT', 'count': zones.get(part),
                                     'overlap': overlap,
                                     'attribution': _attribution(entries, zone, bucket, plan, parts)})
            continue
        if zone in zones:  # WHOLE：整区观测存在才参与
            whole_zones.append(zone)

    if whole_zones:
        try:
            low, high = _whole_interval(whole_zones, zones, pairs)
            region_intervals.append((low, high))
        except DomainError as exc:
            quarantined.append((exc.code, _rows_for(rows, whole_zones)))

    # 归属表里没有的分区编码一律隔离，防止悄悄并入正式结果。
    stray = [r for r in rows if r['kind'] == 'zone' and r['left'] not in claimed]
    if stray:
        quarantined.append(('UNMAPPED_PART', stray))

    result = None
    if region_intervals:
        low, high = combine_regions(region_intervals)
        result = {'bucket': bucket, 'lower': low, 'upper': high, 'width': high - low,
                  'code': 'EXACT' if low == high else 'INTERVAL'}
    return result, pending, attributions, quarantined


def _whole_interval(zone_keys, zones, pairs):
    model = {'zones': {k: zones[k] for k in zone_keys},
             'pairs': {k: v for k, v in pairs.items()
                       if set(k.split('|')) <= set(zone_keys)},
             'triple': None}
    low, high, _, _ = bounds(model)
    return low, high


def apply(observations, entries):
    state = {'calculations': [], 'quarantine': [], 'pending': [], 'attributions': [],
             'business': {}}
    grouped = defaultdict(list)
    for r in observations:
        grouped[r['bucket']].append(r)
    for bucket in sorted(grouped):
        result, pending, attributions, quarantined = rule_bucket(bucket, grouped[bucket], entries)
        for code, qrows in quarantined:
            for r in qrows:
                state['quarantine'].append({'line': r.get('_line', 0), 'id': r.get('id', ''),
                                            'code': code, 'raw': r.get('_raw', {}), 'bucket': bucket})
        state['pending'].extend(pending)
        state['attributions'].extend(attributions)
        if result:
            result['line'] = min(r.get('_line', 0) for r in grouped[bucket])
            state['calculations'].append(result)
    state['business'] = {
        'total': sum(r['lower'] for r in state['calculations']),
        'upper_total': sum(r['upper'] for r in state['calculations']),
        'buckets': len(state['calculations']), 'unit': '客次',
        'pending_buckets': len({p['bucket'] for p in state['pending']}),
        'official_only': True,
    }
    return state
