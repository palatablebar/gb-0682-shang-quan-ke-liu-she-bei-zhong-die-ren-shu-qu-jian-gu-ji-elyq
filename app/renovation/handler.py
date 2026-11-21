"""改造期分区归属与正式人数区间的 HTTP 入口。"""
import base64
from flask import Blueprint, render_template, jsonify, request, Response
from app.errors import DomainError
from app.renovation import service, store
from app.renovation.export_out import bundle

bp = Blueprint('renovation', __name__)


def _body():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise DomainError('PAYLOAD', '请求必须是对象')
    return data


@bp.get('/renovation')
def screen():
    service.seed_default_plan()
    latest = store.get_version() if store.latest_version() else None
    return render_template('renovation.html', parts=store.list_parts(),
                           staged=store.staged_boundaries(), latest=latest,
                           current_version=store.latest_version())


@bp.get('/api/renovation/parts')
def parts_list():
    return jsonify(store.list_parts())


@bp.post('/api/renovation/parts')
def parts_upsert():
    data = _body()
    from app.renovation.partition import validate_entry
    entry = validate_entry(data)
    saved = store.upsert_part(entry, service._now())
    return jsonify(saved)


@bp.post('/api/renovation/base')
def set_base():
    data = _body()
    csv_text = data.get('csv')
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise DomainError('CSV', 'CSV内容必须是文本')
    # 先跑一遍预处理，拒绝无法采信的基础观测。
    service._prepare(csv_text.encode())
    b64 = base64.b64encode(csv_text.encode()).decode('ascii')
    store.save_base(b64)
    return jsonify({'saved': True})


@bp.get('/api/renovation/boundaries')
def boundaries_list():
    return jsonify(store.staged_boundaries())


@bp.post('/api/renovation/boundaries')
def boundaries_upload():
    data = _body()
    csv_text = data.get('csv')
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise DomainError('CSV', 'CSV内容必须是文本')
    return jsonify(service.upload_boundary(csv_text.encode()))


@bp.post('/api/renovation/recompute')
def recompute_only():
    data = _body()
    snapshot, affected, digests = service.recompute(
        affected_only=bool(data.get('affected_only', True)))
    return jsonify({'snapshot': snapshot, 'affected_buckets': affected,
                    'digests': digests, 'published': False})


@bp.post('/api/renovation/publish')
def publish():
    data = _body()
    expected = data.get('expected_version')
    if not isinstance(expected, int):
        raise DomainError('VERSION', '请提交当前正式版本号')
    snap = service.publish(expected, affected_only=bool(data.get('affected_only', True)))
    return jsonify(snap)


@bp.get('/api/renovation/versions')
def versions():
    with store.cursor() as db:
        rows = db.execute('SELECT version,affected_buckets,created_at,boundary_digests '
                          'FROM versions ORDER BY version DESC').fetchall()
    return jsonify([dict(r) for r in rows])


@bp.get('/api/renovation/versions/<int:version>')
def version_detail(version):
    return jsonify(store.get_version(version))


@bp.get('/api/renovation/versions/<int:version>/export.csv')
def export(version):
    snap = store.get_version(version)
    return Response(bundle(snap), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition':
                             'attachment; filename=renovation-v%d.csv' % version})
