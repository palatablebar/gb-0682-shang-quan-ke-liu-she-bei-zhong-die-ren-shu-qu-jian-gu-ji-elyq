'use strict';
const notice = document.querySelector('#notice');

async function call(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.message || ('请求失败 ' + r.status));
  return d;
}

const partForm = document.querySelector('#part-form');
if (partForm) partForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = new FormData(partForm);
  try {
    await call('/api/renovation/parts', {
      part: f.get('part').trim(), zone: f.get('part').trim().charAt(0),
      status: f.get('status'),
      effective_from: f.get('effective_from').trim(),
      effective_to: f.get('effective_to').trim(),
      label: f.get('part').trim(), source: '人工登记',
    });
    notice.textContent = '归属已登记，重算前不会并入正式结果';
    setTimeout(() => location.reload(), 600);
  } catch (err) { notice.textContent = err.message; }
});

const boundaryForm = document.querySelector('#boundary-form');
if (boundaryForm) boundaryForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const csv = new FormData(boundaryForm).get('csv');
  try {
    const d = await call('/api/renovation/boundaries', { csv });
    notice.textContent = d.created
      ? ('已暂存，受影响桶：' + d.affected_buckets.join('、'))
      : '同一记录已存在，未新增暂存、不会产生新版本';
    setTimeout(() => location.reload(), 800);
  } catch (err) { notice.textContent = err.message; }
});

const recomputeBtn = document.querySelector('#recompute');
if (recomputeBtn) recomputeBtn.addEventListener('click', async () => {
  try {
    const d = await call('/api/renovation/recompute', { affected_only: true });
    notice.textContent = '预演完成（未发布）：受影响 ' + d.affected_buckets.length + ' 桶，正式区间 '
      + d.snapshot.calculations.length + ' 条，待确认 ' + d.snapshot.pending.length + ' 条';
  } catch (err) { notice.textContent = err.message; }
});

const publishBtn = document.querySelector('#publish');
if (publishBtn) publishBtn.addEventListener('click', async () => {
  try {
    const d = await call('/api/renovation/publish', {
      expected_version: Number(publishBtn.dataset.rev), affected_only: true,
    });
    notice.textContent = '已发布 V' + d.version + '，旧版本保留可追溯';
    setTimeout(() => location.reload(), 800);
  } catch (err) { notice.textContent = err.message; }
});
