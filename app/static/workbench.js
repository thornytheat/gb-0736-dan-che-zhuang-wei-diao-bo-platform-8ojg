import {$, api, clock, date, notice, post, showError, time} from './view.js';
import * as stages from './stages.js';
import {fillDraft, newLeg, readDraft} from './dispatch.js';
import {fillEditor, handover as renderHandover, readSignoffs} from './handover.js';

const anchors = [...document.querySelectorAll('.rail-nav [data-nav]')];
const keys = anchors.map(link => link.dataset.nav);
let current, meta, data, extra, selected, revision = 0;
let draftRun, previewResult, previewedLegs;
let handoverDraft, handoverResult, payloadId;
let busy = false;

function runId() { return meta.manifest.id; }
function activeId() { return meta.history.find(row => row.active).id; }

function updateMeta() {
  const {manifest, shift} = meta;
  $('shift-name').textContent = shift.name;
  $('shift-range').textContent = `${date(shift.start)} · ${time(shift.start)}–${time(shift.end)}`;
  $('run-short').textContent = runId().slice(0, 12);
  $('footer-state').textContent = runId() === activeId() ? '当前班次' : '历史留档';
  $('footer-meta').textContent = `${manifest.zone} · v${manifest.rule_version} · ${date(manifest.generated_at)} ${clock(manifest.generated_at)}`;
  $('export').href = `/api/runs/${runId()}/evidence.json`;
  $('export').removeAttribute('aria-disabled');
  $('rerun').disabled = runId() !== activeId() || busy;
  for (const button of document.querySelectorAll('#upload-form button')) button.disabled = runId() !== activeId() || busy;
}

function paint() {
  if (!meta || !data) return;
  if (current === 'runs') stages.runs(meta, id => load(id));
  else if (current === 'dispatch') {
    if (draftRun !== runId()) {
      draftRun = runId();
      previewResult = previewedLegs = null;
      fillDraft(data.legs, meta.shift, invalidate);
    }
    stages.dispatch(previewResult || data, meta.shift, Boolean(previewResult));
    $('publish').disabled = !previewedLegs || runId() !== activeId() || busy;
  } else if (current === 'handover') {
    if (handoverDraft !== runId()) {
      handoverDraft = runId();
      handoverResult = null;
      fillEditor(meta.sample_plan, meta.shift, invalidateHandover);
      const cutoff = new Date(Date.parse(meta.shift.end) - 30 * 60000);
      $('handover-cutoff').value = cutoff.toISOString().slice(0, 19);
    }
    if (handoverResult) renderHandover(handoverResult, meta.shift, true);
    else if (data) renderHandover(data, meta.shift, false);
    else {
      for (const id of ['handover-summary', 'handover-legs', 'handover-opening',
        'handover-options', 'handover-demand', 'handover-papers']) $(id).replaceChildren();
      $('handover-state').textContent = '尚未生成交接单';
      $('handover-expiry').textContent = '';
    }
    const evidence = $('handover-evidence');
    const handoverId = handoverResult ? null : (data ? payloadId : null);
    if (handoverId) {
      evidence.href = `/api/handover/${encodeURIComponent(handoverId)}/evidence.json`;
      evidence.removeAttribute('aria-disabled');
    } else {
      evidence.removeAttribute('href');
      evidence.setAttribute('aria-disabled', 'true');
    }
    $('handover-publish').disabled = !handoverResult || runId() !== activeId() || busy;
  } else stages[current](data, meta.shift, extra);
}

async function load(run = selected) {
  const key = location.hash.slice(1);
  current = keys.includes(key) ? key : 'replay';
  const stage = current;
  const token = ++revision;
  selected = run;
  data = null;
  const index = keys.indexOf(current);
  const title = anchors[index].lastChild.textContent.trim();
  $('crumb').textContent = $('stage-title').textContent = title;
  $('stage-label').textContent = `班次流程 · ${index + 1} / ${keys.length}`;
  for (const element of document.querySelectorAll('[data-nav]')) {
    if (element.dataset.nav === current) element.setAttribute('aria-current', 'step');
    else element.removeAttribute('aria-current');
  }
  for (const section of document.querySelectorAll('.stage')) section.hidden = true;
  $('kpis').replaceChildren();
  $('rerun').disabled = true;
  $('export').removeAttribute('href');
  $('export').setAttribute('aria-disabled', 'true');
  notice('正在读取班次…');
  try {
    const runMeta = await api(`/api/runs${run ? `?run=${encodeURIComponent(run)}` : ''}`);
    if (token !== revision) return;
    const suffix = `?run=${encodeURIComponent(runMeta.manifest.id)}`;
    const [payload, slots] = await Promise.all([
      stage === 'runs' ? runMeta : api(`/api/${stage}${suffix}`),
      stage === 'replay' ? api(`/api/demand${suffix}`) : null,
    ]);
    if (token !== revision) return;
    meta = runMeta;
    data = stage === 'runs' ? runMeta : payload.data;
    payloadId = stage === 'handover' ? payload.id : null;
    extra = slots?.data;
    updateMeta();
    paint();
    $(`stage-${stage}`).hidden = false;
    notice();
  } catch (error) {
    if (token === revision) showError(error, true);
  }
}

async function mutate(action, success) {
  if (busy) return;
  busy = true;
  const token = revision;
  const form = current === 'dispatch' ? $('dispatch-form') : current === 'intake' ? $('upload-form') : null;
  if (form) form.inert = true;
  $('rerun').disabled = $('restore').disabled = true;
  notice('正在计算…');
  try {
    const result = await action();
    if (token === revision) await success(result);
  } catch (error) {
    if (token === revision) showError(error);
  } finally {
    if (form) form.inert = false;
    busy = false;
    $('restore').disabled = false;
    if (meta && data) { updateMeta(); paint(); }
  }
}

function invalidate() {
  previewResult = previewedLegs = null;
  notice();
  paint();
}

function invalidateHandover() {
  handoverResult = null;
  notice();
  paint();
}

window.addEventListener('hashchange', () => {
  if (location.hash !== '#main') { stages.clearSlot(); load(); }
});
for (const id of ['intake-filter', 'replay-mode', 'demand-mode', 'inventory-mode']) $(id).addEventListener('change', paint);
$('clear-slot').addEventListener('click', () => { stages.clearSlot(); paint(); });
$('refresh').addEventListener('click', () => { draftRun = handoverDraft = null; stages.clearSlot(); load(null); });
$('rerun').addEventListener('click', () => mutate(() => post('/api/runs/rerun', {expected: runId()}), async result => {
  draftRun = handoverDraft = null;
  await load(null);
  notice(result.created ? '已归档新版本' : '重跑完成 · 沿用同一班次编号');
}));
$('restore').addEventListener('click', () => mutate(() => post('/api/runs/restore', {target: runId(), expected: activeId()}), async () => {
  draftRun = handoverDraft = null;
  await load(null);
  notice('已切回所选班次');
}));
$('upload-form').addEventListener('submit', event => {
  event.preventDefault();
  const file = new FormData(event.currentTarget);
  file.set('expected', runId());
  mutate(() => api('/api/intake', {method: 'POST', body: file}), async result => {
    $('upload-form').reset();
    draftRun = handoverDraft = handoverResult = null;
    await load(null);
    notice(result.created ? '收件完成 · 已归档' : '文件已收件 · 班次编号保持');
  });
});
$('dispatch-form').addEventListener('input', invalidate);
$('dispatch-form').addEventListener('submit', event => {
  event.preventDefault();
  const legs = readDraft();
  mutate(() => post('/api/dispatch/preview', {run: runId(), legs}), result => {
    previewResult = result.dispatch;
    previewedLegs = legs;
    notice('试算完成');
  });
});
$('publish').addEventListener('click', () => mutate(() => post('/api/dispatch/publish', {expected: runId(), legs: previewedLegs}), async () => {
  draftRun = handoverDraft = handoverResult = null;
  await load(null);
  notice('方案已发布');
}));
$('sample-plan').addEventListener('click', () => {
  if (busy) return;
  fillDraft(meta.sample_plan, meta.shift, invalidate);
  invalidate();
});
$('discard').addEventListener('click', () => {
  fillDraft(data.legs, meta.shift, invalidate);
  invalidate();
  notice('已恢复发布方案');
});
$('add-leg').addEventListener('click', () => {
  if (!$('dispatch-form').reportValidity()) return;
  const legs = readDraft();
  if (legs.length >= 20) { notice('最多 20 趟行程', true); return; }
  fillDraft([...legs, newLeg(legs, meta.shift)], meta.shift, invalidate);
  invalidate();
});
$('handover-add').addEventListener('click', () => {
  if (!$('handover-form').reportValidity()) return;
  const rows = readSignoffs();
  fillEditor(meta.sample_plan, meta.shift, invalidateHandover,
    [...rows, {leg: meta.sample_plan[0].id, kind: 'accepted', count: 1,
      at: meta.sample_plan[0].arrive, text: ''}]);
  invalidateHandover();
});
$('handover-form').addEventListener('input', invalidateHandover);
$('handover-form').addEventListener('submit', event => {
  event.preventDefault();
  const payload = {run: runId(), cutoff: apiCutoff(), signoffs: readSignoffs()};
  mutate(() => post('/api/handover/preview', payload), result => {
    handoverResult = result.handover;
    notice('交接单已试算，可发布');
  });
});
$('handover-publish').addEventListener('click', () => mutate(() => post('/api/handover/publish', {
  expected: runId(), cutoff: apiCutoff(), signoffs: readSignoffs(),
}), async result => {
  handoverDraft = null;
  handoverResult = null;
  await load(null);
  notice(result.created ? '交接单已发布 · 旧行程按下一班开始时点失效' : '同一交接单 · 沿用原编号');
}));
function apiCutoff() {
  const value = $('handover-cutoff').value;
  return value ? `${value.length === 16 ? value + ':00' : value}+08:00` : value;
}
load(null);
