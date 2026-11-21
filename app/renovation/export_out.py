"""改造期正式区间快照的归档导出。

与区间页面渲染同一份快照（store.get_version），分区归属（attribution）
随每条区间、待确认证据与隔离记录一起落盘，保证页面与归档证据口径一致。
"""
import csv
import io
import json


def bundle(snapshot):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['记录类型', '版本', '时段', '业务ID', '原因码', '分区归属', '内容'])
    version = snapshot.get('version', 'draft')
    writer.writerow(['版本清单', version, '', '', '',
                     json.dumps({'plan_entries': snapshot.get('entries', []),
                                 'recomputed_buckets': snapshot.get('recomputed_buckets', []),
                                 'kept_buckets': snapshot.get('kept_buckets', []),
                                 'digest': snapshot.get('digest')},
                                ensure_ascii=False, sort_keys=True), ''])
    for row in snapshot.get('calculations', []):
        writer.writerow(['正式区间', version, row['bucket'], row['bucket'], row.get('code', ''),
                         _attribution_of(snapshot, row['bucket']),
                         json.dumps(row, ensure_ascii=False, sort_keys=True)])
    for row in snapshot.get('pending', []):
        writer.writerow(['待确认', version, row['bucket'], row.get('id', ''), row['code'],
                         json.dumps(row.get('attribution', {}), ensure_ascii=False, sort_keys=True),
                         json.dumps(row.get('raw', {}), ensure_ascii=False, sort_keys=True)])
    for row in snapshot.get('quarantine', []):
        writer.writerow(['隔离', version, row.get('bucket', ''), row.get('id', ''), row['code'],
                         '', json.dumps(row.get('raw', {}), ensure_ascii=False, sort_keys=True)])
    biz = snapshot.get('business', {})
    writer.writerow(['对账', version, '', '', '', '',
                     json.dumps(biz, ensure_ascii=False, sort_keys=True)])
    return '﻿' + out.getvalue()


def _attribution_of(snapshot, bucket):
    """同一时段所有分区归属证据共用一份冻结归属，页面与导出取数一致。"""
    evidence = [a for a in snapshot.get('attributions', []) if a['bucket'] == bucket]
    if not evidence:
        return ''
    return json.dumps([a['attribution'] for a in evidence], ensure_ascii=False, sort_keys=True)
