"""改造期分区归属与人数区间裁决。

裁决原则（对应改造期东门被拆成两段、扶梯口仍重叠的场景）：
- 不沿用旧东门整区：整区口径会掩盖两段之间的扶梯口重叠段；
- 不直接把两段相加：相加会把扶梯口算两遍；
- 两段按“同一父区下的分拆分区”建模，观测到扶梯口交集 I 时联合人数为 A1+A2-I，
  未观测到交集时只给保守区间 [max(A1,A2), A1+A2]，不伪造精确人数；
- 归属未确认（pending）的分区只能进入待确认证据，不能并入正式人数区间；
- 改造结束并回（merged / 超出有效窗口）后恢复旧整区口径。
"""
from datetime import datetime
from app.errors import DomainError
from app.modules.m2_mapping.coerce import instant

STATUSES = ('pending', 'confirmed', 'merged')
ZONES = ('A', 'B', 'C')


def parse_dt(value):
    """分区有效窗口的时间点必须带时区，统一转 UTC。"""
    if value in (None, ''):
        return None
    return instant(value)


def validate_entry(entry):
    zone = entry.get('zone', '').strip().upper()
    part = entry.get('part', '').strip().upper()
    if zone not in ZONES:
        raise DomainError('CONSTRAINT', '父区必须为 A、B、C 之一')
    if not part or not part.startswith(zone):
        raise DomainError('CONSTRAINT', '分拆分区编码必须以父区编码开头，如 A1')
    status = entry.get('status', 'pending')
    if status not in STATUSES:
        raise DomainError('CONSTRAINT', '归属状态必须为 pending/confirmed/merged')
    effective_from = parse_dt(entry.get('effective_from'))
    effective_to = parse_dt(entry.get('effective_to'))
    if effective_from and effective_to and effective_to <= effective_from:
        raise DomainError('CONSTRAINT', '有效窗口结束时间必须晚于开始时间')
    return {
        'zone': zone, 'part': part,
        'label': entry.get('label', '').strip() or part,
        'status': status,
        'effective_from': effective_from,
        'effective_to': effective_to,
        'source': str(entry.get('source', '')).strip(),
    }


def _as_dt(bucket):
    return datetime.fromisoformat(bucket)


def active_entries(entries, zone, bucket):
    """某小时桶内、某父区下仍处于有效窗口且未并回的分区归属记录。"""
    moment = _as_dt(bucket)
    out = []
    for e in entries:
        if e['zone'] != zone or e['status'] == 'merged':
            continue
        if e['effective_from'] and moment < _as_dt(e['effective_from']):
            continue
        if e['effective_to'] and moment >= _as_dt(e['effective_to']):
            continue
        out.append(e)
    return sorted(out, key=lambda e: e['part'])


def zone_plan(entries, zone, bucket):
    """返回 (口径, 生效分区)。

    WHOLE  改造前或并回后，沿用整区；
    SPLIT  分区归属全部确认，按分段联合裁决；
    PENDING 存在未确认归属，该桶不得进入正式结果。
    """
    active = active_entries(entries, zone, bucket)
    if not active:
        return 'WHOLE', [zone]
    if any(e['status'] == 'pending' for e in active):
        return 'PENDING', [e['part'] for e in active]
    return 'SPLIT', [e['part'] for e in active]


def split_interval(zone_counts, pairs, part_x, part_y):
    """两段分拆分区的联合人数区间。

    zone_counts: 分区编码 -> 该分区观测人数；
    pairs: 'X|Y' -> 观测到的重叠（扶梯口）人数，缺失表示未观测交集。
    """
    if part_x not in zone_counts or part_y not in zone_counts:
        raise DomainError('REFERENCE', '分拆分区观测不完整，缺少分段人数')
    x, y = zone_counts[part_x], zone_counts[part_y]
    key = '|'.join(sorted([part_x, part_y]))
    overlap = pairs.get(key)
    if overlap is not None:
        if overlap > min(x, y):
            raise DomainError('CONSTRAINT', '扶梯口重叠人数超出分段观测')
        low = high = x + y - overlap
    else:
        overlap = None
        low, high = max(x, y), x + y
    return low, high, overlap


def combine_regions(region_intervals):
    """多个父区（自身可能是区间）在跨区交集未观测时的最紧保守联合区间。

    下界取各区域最大者，上界求和——与数据字典中两区域无交集观测时
    [max(A,B), A+B] 的口径保持一致，不假定任何未观测交集。
    """
    if not region_intervals:
        raise DomainError('REFERENCE', '没有可联合的区域观测')
    low = max(lo for lo, _ in region_intervals)
    high = sum(hi for _, hi in region_intervals)
    return low, high
