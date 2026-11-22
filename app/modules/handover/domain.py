from collections import Counter, defaultdict
from datetime import timedelta

from app.clock import instant, stamp
from app.errors import DomainError

# 交接单里每个动作允许出现的签收状态
SIGNOFF_KINDS = {'accepted', 'rejected'}


def nxt_start(nxt):
    return instant(nxt['start'])


def validate_shift(shift):
    """交接依赖下一班窗口；样例之外的班次可以不带 next_shift。"""
    nxt = getattr(shift, 'next_shift', None)
    if nxt is None:
        raise DomainError('NEXT_SHIFT_MISSING')
    start, end = instant(nxt['start']), instant(nxt['end'])
    if not shift.end <= start < end:
        raise DomainError('NEXT_SHIFT_RANGE')
    for code, config in nxt['trucks'].items():
        if code not in shift.trucks:
            raise DomainError('NEXT_SHIFT_TRUCK', truck=code)
        if instant(config['available_at']) < start:
            raise DomainError('NEXT_SHIFT_RANGE', truck=code)
        for field in ('crew', 'station'):
            if not isinstance(config.get(field), str) or not config[field].strip():
                raise DomainError('NEXT_SHIFT_TRUCK', truck=code, field=field)
    return nxt


def validate_cutoff(shift, nxt, cutoff):
    try:
        point = instant(cutoff)
    except DomainError:
        raise DomainError('BAD_TIME', field='cutoff') from None
    if not shift.start < point <= nxt_start(nxt):
        raise DomainError('CUTOFF_OUTSIDE', cutoff=stamp(point),
                          start=stamp(shift.start), end=nxt['start'])
    return point


def validate_signoffs(shift, nxt, legs, signoffs):
    """签收单结构校验并按行程归并；同一纸面单据重试只保留首次确认。"""
    if not isinstance(signoffs, list) or not signoffs:
        raise DomainError('SIGNOFF_EMPTY')
    leg_by_id = {leg['id']: leg for leg in legs}
    grouped = defaultdict(list)
    seen_keys = set()
    for index, item in enumerate(signoffs, 1):
        if not isinstance(item, dict):
            raise DomainError('BAD_SIGNOFF', index=index)
        required = {'leg', 'kind', 'count', 'at', 'text'}
        if set(item) != required:
            raise DomainError('BAD_SIGNOFF', index=index)
        leg = item['leg']
        if leg not in leg_by_id:
            raise DomainError('SIGNOFF_UNKNOWN_LEG', index=index, leg=leg)
        if item['kind'] not in SIGNOFF_KINDS:
            raise DomainError('BAD_SIGNOFF_KIND', index=index, leg=leg)
        count = item['count']
        if type(count) is not int or count < 1:
            raise DomainError('BAD_SIGNOFF', index=index, leg=leg, field='count')
        if count > leg_by_id[leg]['count']:
            raise DomainError('SIGNOFF_OVER_COUNT', leg=leg, accepted=count,
                              planned=leg_by_id[leg]['count'])
        try:
            at = instant(item['at'])
        except DomainError:
            raise DomainError('BAD_TIME', field='at', leg=leg) from None
        if not shift.start <= at <= nxt_start(nxt):
            raise DomainError('SIGNOFF_OUTSIDE', index=index, leg=leg)
        text = item['text']
        if not isinstance(text, str) or not text.strip():
            raise DomainError('SIGNOFF_TEXT_REQUIRED', index=index, leg=leg)
        # 行程、签收时点、原文、辆数构成留档键；完全相同的重试视为同一纸面单据
        key = (leg, stamp(at), text, count)
        duplicate = key in seen_keys
        seen_keys.add(key)
        grouped[leg].append({'leg': leg, 'kind': item['kind'], 'count': count,
                             'at': stamp(at), 'text': text, 'duplicate': duplicate,
                             'index': index})
    # 同行程内按签收时点、提交序号裁决；互相矛盾的结论全部拒绝并留痕
    conflicts = []
    for leg, rows in grouped.items():
        rows.sort(key=lambda row: (instant(row['at']), row['index']))
        kinds = {row['kind'] for row in rows if not row['duplicate']}
        if len(kinds) > 1:
            conflicts.append(leg)
    if conflicts:
        raise DomainError('SIGNOFF_CONFLICT', legs=sorted(conflicts))
    return grouped


def leg_execution(legs, signoff_groups, cutoff):
    """逐行程判定已执行/在途/未执行，并核对累计签收数量不超过行程辆数。"""
    rows = []
    point = instant(cutoff) if isinstance(cutoff, str) else cutoff
    for leg in legs:
        papers = signoff_groups.get(leg['id'], [])
        fresh = [paper for paper in papers if not paper['duplicate']]
        accepted = sum(paper['count'] for paper in fresh if paper['kind'] == 'accepted')
        rejected = sum(paper['count'] for paper in fresh if paper['kind'] == 'rejected')
        if accepted > leg['count']:
            raise DomainError('SIGNOFF_OVER_COUNT', leg=leg['id'],
                              accepted=accepted, planned=leg['count'])
        last_paper = max((instant(paper['at']) for paper in papers), default=None)
        depart, arrive = instant(leg['depart']), instant(leg['arrive'])
        if accepted == leg['count']:
            status = 'done'
        elif accepted:
            status = 'partial'
        elif arrive <= point:
            # 车辆已到站但没有有效签收：车上留存，不算已卸
            status = 'arrived_pending'
        elif depart <= point < arrive:
            status = 'in_transit'
        else:
            status = 'pending'
        remaining = leg['count'] - accepted
        rows.append({'leg': leg['id'], 'truck': leg['truck'], 'from': leg['from'], 'to': leg['to'],
                     'depart': leg['depart'], 'arrive': leg['arrive'], 'count': leg['count'],
                     'status': status, 'unloaded': accepted, 'rejected': rejected,
                     'remaining_on_truck': remaining,
                     'last_signoff_at': stamp(last_paper) if last_paper else None})
    return rows


def state_at(shift, timeline, point):
    """取交接时点（含）之前各站最后状态；没有事件的站回退到期初。"""
    states = {}
    road = shift.opening_road
    for row in timeline:
        if instant(row['at']) > point:
            break
        states[row['station']] = row['after']
        road = row['road']
    result = {}
    for code, config in shift.stations.items():
        if code in states:
            result[code] = dict(states[code])
        else:
            result[code] = {'docked': config['opening'], 'held': 0,
                            'capacity': config['opening'], 'free': 0}
    return result, road


def handover_replay(shift, base_timeline, execution, point):
    """把实际装车、签收卸车插入基准时间线，按既有四级顺序重放到交接时点。

    基准时间线只含容量切换和租还尝试；交接动作沿用同秒
    “容量 → 卸车 → 装车 → 租还”的裁决顺序，保证库存与正式回放口径一致。
    """
    moves = []
    for row in execution:
        depart = instant(row['depart'])
        if depart <= point:
            moves.append((depart, 2, row['leg'], {'station': row['from'], 'count': row['count'],
                                                   'kind': 'load', 'line': None}))
        if row['unloaded']:
            last = instant(row['last_signoff_at']) if row['last_signoff_at'] else depart
            if last <= point:
                moves.append((last, 1, row['leg'], {'station': row['to'], 'count': row['unloaded'],
                                                     'kind': 'unload', 'line': None}))
    steps = []
    for row in base_timeline:
        at = instant(row['at'])
        if at <= point:
            priority = 0 if row['kind'] == 'capacity' else 3
            steps.append((at, priority, row['id'], row))
    steps.extend(moves)
    steps.sort(key=lambda item: item[:3])

    docks = {code: {'docked': cfg['opening'], 'held': 0, 'capacity': cfg['opening']}
             for code, cfg in shift.stations.items()}
    road, transit = shift.opening_road, 0
    deliveries = defaultdict(int)
    for at, priority, key, row in steps:
        station = row['station']
        dock = docks[station]
        if priority == 0:
            # 容量切换直接沿用基准重放已校验的状态
            dock['capacity'] = row['after']['capacity']
            dock['docked'], dock['held'] = row['after']['docked'], row['after']['held']
        elif priority == 1:
            if dock['capacity'] - dock['docked'] < row['count']:
                raise DomainError('DROP_FULL', leg=key, station=station, at=stamp(at),
                                  available=dock['capacity'] - dock['docked'],
                                  requested=row['count'])
            dock['docked'] += row['count']
            transit -= row['count']
            deliveries[station] += row['count']
        elif priority == 2:
            if dock['docked'] < row['count']:
                raise DomainError('PICKUP_SHORT', leg=key, station=station, at=stamp(at),
                                  available=dock['docked'], requested=row['count'])
            dock['docked'] -= row['count']
            total = dock['docked'] + dock['held']
            dock['docked'] = min(dock['capacity'], total)
            dock['held'] = total - dock['docked']
            transit += row['count']
        else:
            kind = row['kind']
            if dock['capacity'] == 0:
                continue
            if kind == 'rent':
                if not dock['docked']:
                    continue
                dock['docked'] -= 1
                total = dock['docked'] + dock['held']
                dock['docked'] = min(dock['capacity'], total)
                dock['held'] = total - dock['docked']
                road += 1
            else:
                if dock['capacity'] - dock['docked'] == 0 or not road:
                    continue
                dock['docked'] += 1
                road -= 1
    for dock in docks.values():
        dock['free'] = dock['capacity'] - dock['docked']
    return docks, road, transit, dict(deliveries)


def apply_handover(shift, execution, point, base_timeline):
    """下一班期初库存由“基准租还 + 实际装卸”迷你重放推导，不照抄计划期末。"""
    states, road, transit, deliveries = handover_replay(
        shift, base_timeline, execution, point)
    # 已装车但未签收的车辆才在途；交接时点尚未装车的车仍停在始发站
    carried = sum(row['remaining_on_truck'] for row in execution
                  if instant(row['depart']) <= point)
    if transit != carried:
        raise DomainError('INVENTORY_MISMATCH', 409, transit=transit, carried=carried)
    fleet = sum(item['docked'] + item['held'] for item in states.values()) + road + transit
    expected = sum(c['opening'] for c in shift.stations.values()) + shift.opening_road
    if fleet != expected:
        raise DomainError('INVENTORY_MISMATCH', 409, fleet=fleet, expected=expected)
    return states, road, carried, deliveries


def carry_demand(replay_plan, replay_base, point, deliveries):
    """未成交按原编号结转；方案下已恢复成交的尝试不再结转，附站点缓解量。"""
    plan_ids = {row['id']: row for row in replay_plan['timeline']
                if row['kind'] in {'rent', 'return'}}
    carried = []
    for row in replay_base['timeline']:
        if row['kind'] not in {'rent', 'return'} or not row['code']:
            continue
        if instant(row['at']) > point:
            continue
        current = plan_ids.get(row['id'])
        if current is not None and current['code'] is None:
            continue
        carried.append({'id': row['id'], 'station': row['station'], 'kind': row['kind'],
                        'at': row['at'], 'code': row['code'], 'line': row['line'],
                        'relieved': deliveries.get(row['station'], 0)})
    carried.sort(key=lambda row: (instant(row['at']), row['id']))
    reasons = Counter(row['code'] for row in carried)
    by_station = Counter(row['station'] for row in carried)
    return {'carried': carried, 'total': len(carried), 'reasons': dict(reasons),
            'by_station': dict(by_station)}


def _slot(nxt, truck_id, earliest, travel):
    start = max(earliest, instant(nxt['trucks'][truck_id]['available_at']))
    return start, start + travel


def truck_handover_positions(nxt, execution, cutoff):
    """每辆车在交接时点的实际驻站与最早可发车时间。

    已出发的车按最后一个已执行行程的终点定位；未出发的车回退到下一班驻站。
    """
    positions = {code: [cfg['station'], instant(cfg['available_at'])]
                 for code, cfg in nxt['trucks'].items()}
    for row in execution:
        truck = row['truck']
        if truck not in positions or instant(row['depart']) > cutoff:
            continue
        if instant(row['arrive']) <= cutoff:
            positions[truck] = [row['to'], instant(row['arrive'])]
        else:
            positions[truck] = [row['from'], instant(row['depart'])]
    return {code: (station, earliest) for code, (station, earliest) in positions.items()}


def original_routes(shift, nxt, execution, cutoff):
    """方案一：按下一班窗口续跑原路线剩余站点，只保留已装车且仍有车载的行程。"""
    travel = timedelta(minutes=shift.min_travel_minutes)
    truck_pos = truck_handover_positions(nxt, execution, cutoff)
    plan = []
    seq = 0
    cutoff_point = cutoff if isinstance(cutoff, str) else stamp(cutoff)
    for row in execution:
        if (row['remaining_on_truck'] <= 0 or row['truck'] not in truck_pos
                or instant(row['depart']) > instant(cutoff_point)):
            continue
        seq += 1
        truck = row['truck']
        origin, earliest = truck_pos[truck]
        # 车辆已抵达原路线目的站时，剩余车在该站等待下一班调度，不生成同站往返
        if origin == row['to']:
            continue
        start, end = _slot(nxt, truck, earliest, travel)
        plan.append({'id': f"H-{seq:02d}", 'truck': truck, 'from': origin, 'to': row['to'],
                     'depart': stamp(start), 'arrive': stamp(end),
                     'count': row['remaining_on_truck'], 'source_leg': row['leg'],
                     'scheme': 'original'})
        truck_pos[truck] = (row['to'], end)
    return plan


def rerouted_routes(shift, nxt, execution, opening, carried_demand, cutoff):
    """方案二：按下一班缺车程度重排，缺车 = 未成交借车笔数，缺车多的站点优先。"""
    travel = timedelta(minutes=shift.min_travel_minutes)
    deficit = Counter()
    for row in carried_demand['carried']:
        if row['kind'] == 'rent' and row['code'] in {'NO_BIKE', 'STATION_CLOSED'}:
            deficit[row['station']] += 1
    # 主排序只看下一班缺车笔数；笔数相同时按站号字典序，保证路线确定可复算
    ranked = sorted(shift.stations, key=lambda code: (-deficit[code], code))
    truck_pos = truck_handover_positions(nxt, execution, cutoff)
    plan, seq = [], 0
    cutoff_point = cutoff if isinstance(cutoff, str) else stamp(cutoff)
    for row in execution:
        if (row['remaining_on_truck'] <= 0 or row['truck'] not in truck_pos
                or instant(row['depart']) > instant(cutoff_point)):
            continue
        truck = row['truck']
        origin, earliest = truck_pos[truck]
        target = next(code for code in ranked if code != origin)
        seq += 1
        start, end = _slot(nxt, truck, earliest, travel)
        plan.append({'id': f"R-{seq:02d}", 'truck': truck, 'from': origin, 'to': target,
                     'depart': stamp(start), 'arrive': stamp(end),
                     'count': row['remaining_on_truck'], 'source_leg': row['leg'],
                     'scheme': 'rerouted', 'reason': f"缺车 {deficit[target]} 笔"})
        truck_pos[truck] = (target, end)
    return plan, dict(deficit)


def build_handover(shift, snapshot, payload):
    nxt = validate_shift(shift)
    cutoff = validate_cutoff(shift, nxt, payload['cutoff'])
    legs = snapshot['dispatch']['legs']
    groups = validate_signoffs(shift, nxt, legs, payload['signoffs'])
    execution = leg_execution(legs, groups, cutoff)
    states, road, carried_bikes, deliveries = apply_handover(
        shift, execution, cutoff, snapshot['replay']['base']['timeline'])
    demand = carry_demand(snapshot['replay']['plan'], snapshot['replay']['base'],
                          cutoff, deliveries)
    original = original_routes(shift, nxt, execution, cutoff)
    rerouted, deficit = rerouted_routes(shift, nxt, execution, states, demand, cutoff)
    receivers = {}
    for code, config in nxt['trucks'].items():
        load = sum(row['remaining_on_truck'] for row in execution
                   if row['truck'] == code and instant(row['depart']) <= cutoff)
        receivers[code] = {'crew': config['crew'], 'bikes': load,
                           'available_at': config['available_at'],
                           'station': config['station']}
    return {
        'cutoff': stamp(cutoff),
        'expires_at': nxt['start'],
        'next_shift': {'name': nxt['name'], 'start': nxt['start'], 'end': nxt['end'],
                       'trucks': nxt['trucks']},
        'legs': execution,
        'receivers': receivers,
        'opening': {code: {'docked': item['docked'], 'held': item['held'],
                           'capacity': item['capacity'], 'free': item['free']}
                    for code, item in states.items()},
        'road_bikes': road,
        'transit_bikes': carried_bikes,
        'carried_bikes': carried_bikes,
        'deliveries': deliveries,
        'demand': demand,
        'deficit': deficit,
        'options': {'original': original, 'rerouted': rerouted},
        'signoffs': [paper for papers in groups.values() for paper in papers],
        'fleet': sum(item['docked'] + item['held'] for item in states.values()) + road + carried_bikes,
    }
