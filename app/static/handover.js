import {$, clock, label, node, table, time} from './view.js';

const STATUS = {done: '已执行完', partial: '部分签收', arrived_pending: '到站待签',
  in_transit: '在途', pending: '未执行'};
const local = value => value ? new Date(Date.parse(value) + 8 * 3600000).toISOString().slice(0, 19) : '';
const apiTime = value => value ? `${value.length === 16 ? value + ':00' : value}+08:00` : '';

export function readSignoffs() {
  return [...$('handover-editor').querySelectorAll('tbody tr')].map(row => {
    const get = key => row.querySelector(`[data-field="${key}"]`).value;
    return {leg: get('leg'), kind: get('kind'),
      count: get('count') === '' ? '' : Number(get('count')),
      at: apiTime(get('at')), text: get('text')};
  });
}

export function fillEditor(legs, shift, onEdit, rows = null) {
  const choices = legs.map(leg => [leg.id, `${leg.id} · ${shift.stations[leg.to].name}`]);
  const papers = rows || legs.map(leg => ({leg: leg.id, kind: 'accepted', count: leg.count,
    at: leg.arrive, text: ''}));
  table('handover-editor', ['行程', '签收结论', '确认辆数', '签收时点', '签收原文', '操作'],
    papers.map((paper, index) => {
      const leg = legs.find(item => item.id === paper.leg) || legs[0];
      const legSelect = node('select');
      legSelect.dataset.field = 'leg';
      legSelect.setAttribute('aria-label', `第 ${index + 1} 张签收的行程`);
      for (const [value, name] of choices) {
        const option = node('option', name);
        option.value = value;
        option.selected = value === paper.leg;
        legSelect.append(option);
      }
      const kind = node('select');
      kind.dataset.field = 'kind';
      kind.setAttribute('aria-label', `第 ${index + 1} 张签收的结论`);
      for (const [value, name] of [['accepted', '确认卸车'], ['rejected', '拒收']]) {
        const option = node('option', name);
        option.value = value;
        option.selected = value === paper.kind;
        kind.append(option);
      }
      const count = node('input');
      count.type = 'number';
      count.min = '1';
      count.value = paper.count;
      count.dataset.field = 'count';
      count.required = true;
      const at = node('input');
      at.type = 'datetime-local';
      at.step = 'any';
      at.value = local(paper.at);
      at.dataset.field = 'at';
      at.required = true;
      const text = node('input');
      text.type = 'text';
      text.value = paper.text || '';
      text.placeholder = '纸质签收原文，必填';
      text.dataset.field = 'text';
      text.required = true;
      const remove = node('button', '移除', 'link');
      remove.type = 'button';
      remove.addEventListener('click', () => {
        const next = readSignoffs().filter((_, rowIndex) => rowIndex !== index);
        fillEditor(legs, shift, onEdit, next);
        onEdit();
      });
      return [legSelect, kind, count, at, text, remove];
    }), '先新增签收单');
  for (const [index, row] of [...$('handover-editor').querySelectorAll('tbody tr')].entries()) {
    row.dataset.index = String(index);
  }
}

function summaryCell(title, value, tone = '', aside = '') {
  const cell = node('div', '', 'diff-cell');
  cell.append(node('span', title, 'subtle'), node('strong', value, `num ${tone}`));
  if (aside) cell.append(node('span', aside, 'subtle'));
  return cell;
}

function optionRows(data, shift, scheme, title) {
  const legs = data.options[scheme];
  const wrap = node('div', '', 'panel__body');
  wrap.append(node('p', title, 'subtle'));
  const host = node('div');
  host.id = `handover-${scheme}`;
  wrap.append(host);
  table(`handover-${scheme}`,
    ['行程', '车辆', '从', '到', '出发', '到达', '辆数', '依据'],
    legs.map(leg => [leg.id, leg.truck, shift.stations[leg.from].name,
      shift.stations[leg.to].name, time(leg.depart), time(leg.arrive), leg.count,
      leg.reason || `接续原行程 ${leg.source_leg}`]), '无剩余车辆，无需续跑');
  return wrap;
}

export function handover(data, shift, draft) {
  $('handover-state').textContent = draft ? '草稿试算 · 未发布' : '已发布交接单';
  $('handover-expiry').textContent =
    `旧行程失效时点：${time(data.expires_at)}（下一班开始即失效） · 交接时点 ${time(data.cutoff)}`;
  $('handover-summary').replaceChildren(
    summaryCell('下一班在途车', data.transit_bikes, 'warn', '辆 · 随车交接'),
    summaryCell('在骑车辆', data.road_bikes, '', '辆'),
    summaryCell('全网守恒', data.fleet, 'ok', '辆'),
    summaryCell('结转未成交', data.demand.total, data.demand.total ? 'warn' : 'ok', '笔'),
    summaryCell('接收车辆', Object.values(data.receivers).reduce((s, r) => s + r.bikes, 0), '', '辆'));
  table('handover-legs', ['行程', '车辆', '路线', '装车', '签收', '状态', '已卸', '拒收', '车上剩余'],
    data.legs.map(leg => [leg.leg, leg.truck,
      `${shift.stations[leg.from].name} → ${shift.stations[leg.to].name}`,
      leg.count, STATUS[leg.status], leg.status, leg.unloaded, leg.rejected,
      leg.remaining_on_truck]), '没有行程');
  table('handover-opening', ['站点 / 接收人', '桩上', '暂存', '容量', '空桩', '接收车辆'],
    [...Object.entries(data.opening).map(([code, row]) =>
      [shift.stations[code].name, row.docked, row.held, row.capacity, row.free, '—']),
    ...Object.entries(data.receivers).map(([truck, row]) =>
      [`${truck} · ${row.crew}`, '—', '—', '—', `驻 ${shift.stations[row.station].name}`, row.bikes])],
    '没有库存记录');
  const box = $('handover-options');
  box.replaceChildren();
  for (const [scheme, title] of [['original', '方案一 · 按原路线跑完余下站点'],
                                  ['rerouted', '方案二 · 按下一班缺车重排']]) {
    const fragment = document.createDocumentFragment();
    optionRows(data, shift, scheme, title).forEach(nodeEl => fragment.append(nodeEl));
    box.append(fragment);
  }
  table('handover-demand', ['事件号', '站点', '尝试', '时点', '未成交原因', '本站调入缓解'],
    data.demand.carried.map(row => [row.id, shift.stations[row.station].name,
      row.kind === 'rent' ? '借车' : '还车', clock(row.at), label(row.code), row.relieved]),
    '没有结转的未成交');
  table('handover-papers', ['行程', '结论', '辆数', '签收时点', '原文', '重试'],
    data.signoffs.map(paper => [paper.leg, paper.kind === 'accepted' ? '确认卸车' : '拒收',
      paper.count, clock(paper.at), paper.text, paper.duplicate ? '重复上报' : '首次']),
    '没有签收单');
}
