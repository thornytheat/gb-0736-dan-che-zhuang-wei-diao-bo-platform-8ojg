import json

import pytest

from app.errors import DomainError
from app.clock import instant
from app.modules.archive.pipeline import run_shift
from app.modules.archive.service import sample_plan
from app.modules.handover.domain import build_handover
from app.shift import load_shift

CUTOFF = '2026-11-20T09:30:00+08:00'


def paper(leg, kind, count, at, text):
    return {'leg': leg, 'kind': kind, 'count': count, 'at': at, 'text': text}


@pytest.fixture(scope='session')
def shift(sources):
    return load_shift(sources['shift'])


@pytest.fixture(scope='session')
def snap(sources):
    return run_shift(sources, sample_plan())


def standard_order(extra_text='东门地铁纸质签收 3 辆，T-01 首趟'):
    return {'cutoff': CUTOFF, 'signoffs': [
        paper('M-01', 'accepted', 3, '2026-11-20T08:20:00+08:00', extra_text),
        paper('M-02', 'accepted', 1, '2026-11-20T09:12:00+08:00', '江湾广场纸质签收 1 辆'),
    ]}


def test_execution_sheet_marks_done_partial_and_remaining(shift, snap):
    out = build_handover(shift, snap, standard_order())
    legs = {row['leg']: row for row in out['legs']}
    assert legs['M-01']['status'] == 'done'
    assert (legs['M-01']['unloaded'], legs['M-01']['remaining_on_truck']) == (3, 0)
    assert legs['M-02']['status'] == 'partial'
    assert (legs['M-02']['unloaded'], legs['M-02']['remaining_on_truck']) == (1, 1)


def test_repeated_signoff_is_counted_once_but_archived(shift, snap):
    order = standard_order()
    # 同一张纸面单据重试：行程、时点、原文、辆数完全一致
    order['signoffs'].insert(1, paper('M-01', 'accepted', 3, '2026-11-20T08:20:00+08:00',
                                      '东门地铁纸质签收 3 辆，T-01 首趟'))
    out = build_handover(shift, snap, order)
    m01 = next(row for row in out['legs'] if row['leg'] == 'M-01')
    assert m01['unloaded'] == 3 and m01['remaining_on_truck'] == 0
    papers = [p for p in out['signoffs'] if p['leg'] == 'M-01']
    assert len(papers) == 2 and [p['duplicate'] for p in papers] == [False, True]
    # 原文逐字保留，重复件也不能误删
    assert all(p['text'] == '东门地铁纸质签收 3 辆，T-01 首趟' for p in papers)


def test_next_shift_opening_inventory_conserves_fleet(shift, snap):
    out = build_handover(shift, snap, standard_order())
    station_total = sum(row['docked'] + row['held'] for row in out['opening'].values())
    assert station_total + out['road_bikes'] + out['transit_bikes'] == out['fleet'] == 33
    # M-01 的 3 辆落到 002、M-02 的 1 辆落到 001，车上还剩 1 辆在途
    assert out['deliveries'] == {'002': 3, '001': 1}
    assert out['carried_bikes'] == 1 and out['transit_bikes'] == 1


def test_remaining_bikes_handed_to_named_next_shift_crew(shift, snap):
    out = build_handover(shift, snap, standard_order())
    assert out['receivers']['T-02'] == {
        'crew': '午班 T-02 沈屿', 'bikes': 1, 'station': '003',
        'available_at': '2026-11-20T10:00:00+08:00'}
    assert out['receivers']['T-01']['bikes'] == 0


def test_missed_demand_carries_original_ids_and_relief(shift, snap):
    out = build_handover(shift, snap, standard_order())
    demand = out['demand']
    ids = [row['id'] for row in demand['carried']]
    # 已被方案恢复成交的尝试不结转；未成交保留原编号
    assert '0008' not in ids and '0006' in ids
    row = next(item for item in demand['carried'] if item['id'] == '0006')
    assert row['code'] == 'NO_BIKE' and row['relieved'] == 3
    assert demand['reasons'] == {'NO_BIKE': 1, 'NO_DOCK': 3, 'STATION_CLOSED': 2}


def test_old_legs_expire_when_next_shift_starts(shift, snap):
    out = build_handover(shift, snap, standard_order())
    assert out['expires_at'] == '2026-11-20T10:00:00+08:00'
    assert out['cutoff'] == '2026-11-20T01:30:00Z'


def test_original_and_rerouted_options_target_different_stations(shift, snap):
    out = build_handover(shift, snap, standard_order())
    original, rerouted = out['options']['original'], out['options']['rerouted']
    # T-02 已抵达原路线目的站 001，剩余 1 辆在站内等候，原方案不生成同站往返
    assert original == []
    # 重排按缺车笔数：002 有缺车信号，把 001 的剩余车调往 002
    assert len(rerouted) == 1
    assert rerouted[0]['from'] == '001' and rerouted[0]['to'] == '002'
    assert rerouted[0]['reason'] == '缺车 1 笔'
    assert rerouted[0]['depart'] >= '2026-11-20T02:00:00Z' and rerouted[0]['count'] == 1


def test_original_route_continues_when_truck_still_short_of_target(shift, snap):
    # M-02 尚在途（未到站）：原方案从当前位置续跑至原目的站 001
    order = {'cutoff': '2026-11-20T09:05:00+08:00', 'signoffs': [
        paper('M-01', 'accepted', 3, '2026-11-20T08:20:00+08:00', 'a')]}
    out = build_handover(shift, snap, order)
    original = out['options']['original']
    assert len(original) == 1 and original[0]['to'] == '001'
    assert original[0]['from'] == '003' and original[0]['source_leg'] == 'M-02'


def test_completed_legs_are_not_rescheduled(shift, snap):
    order = {'cutoff': CUTOFF, 'signoffs': [
        paper('M-01', 'accepted', 3, '2026-11-20T08:20:00+08:00', 'a'),
        paper('M-02', 'accepted', 2, '2026-11-20T09:12:00+08:00', 'b')]}
    out = build_handover(shift, snap, order)
    assert out['options']['original'] == [] and out['options']['rerouted'] == []
    assert out['carried_bikes'] == 0


def test_rejected_signoff_keeps_bikes_on_truck(shift, snap):
    order = {'cutoff': CUTOFF, 'signoffs': [
        paper('M-01', 'accepted', 3, '2026-11-20T08:20:00+08:00', 'a'),
        paper('M-02', 'rejected', 2, '2026-11-20T09:12:00+08:00', '现场拒收，桩位冲突')]}
    out = build_handover(shift, snap, order)
    m02 = next(row for row in out['legs'] if row['leg'] == 'M-02')
    assert m02['unloaded'] == 0 and m02['rejected'] == 2
    assert m02['remaining_on_truck'] == 2 and m02['status'] == 'arrived_pending'
    assert out['receivers']['T-02']['bikes'] == 2


def test_contradictory_signoffs_are_rejected(shift, snap):
    order = {'cutoff': CUTOFF, 'signoffs': [
        paper('M-01', 'accepted', 3, '2026-11-20T08:20:00+08:00', 'a'),
        paper('M-01', 'rejected', 3, '2026-11-20T08:25:00+08:00', 'b')]}
    with pytest.raises(DomainError) as caught:
        build_handover(shift, snap, order)
    assert caught.value.code == 'SIGNOFF_CONFLICT'
    assert caught.value.details == {'legs': ['M-01']}


def test_signoff_count_cannot_exceed_leg_load(shift, snap):
    order = {'cutoff': CUTOFF, 'signoffs': [
        paper('M-01', 'accepted', 4, '2026-11-20T08:20:00+08:00', 'a')]}
    with pytest.raises(DomainError, match='SIGNOFF_OVER_COUNT'):
        build_handover(shift, snap, order)


def test_blank_signoff_text_is_refused(shift, snap):
    order = {'cutoff': CUTOFF, 'signoffs': [
        paper('M-01', 'accepted', 3, '2026-11-20T08:20:00+08:00', '   ')]}
    with pytest.raises(DomainError, match='SIGNOFF_TEXT_REQUIRED'):
        build_handover(shift, snap, order)


def test_unknown_leg_and_bad_kind_are_rejected(shift, snap):
    order = {'cutoff': CUTOFF, 'signoffs': [
        paper('M-99', 'accepted', 1, '2026-11-20T08:20:00+08:00', 'a')]}
    with pytest.raises(DomainError, match='SIGNOFF_UNKNOWN_LEG'):
        build_handover(shift, snap, order)
    bad = standard_order()
    bad['signoffs'][0]['kind'] = 'maybe'
    with pytest.raises(DomainError, match='BAD_SIGNOFF_KIND'):
        build_handover(shift, snap, bad)


def test_cutoff_must_lie_between_shifts(shift, snap):
    order = standard_order()
    order['cutoff'] = '2026-11-20T07:30:00+08:00'
    with pytest.raises(DomainError, match='CUTOFF_OUTSIDE'):
        build_handover(shift, snap, order)


def test_empty_signoff_list_is_rejected(shift, snap):
    with pytest.raises(DomainError, match='SIGNOFF_EMPTY'):
        build_handover(shift, snap, {'cutoff': CUTOFF, 'signoffs': []})


def test_leg_not_loaded_by_cutoff_stays_at_origin_station(shift, snap):
    # 08:35 早于 M-02 的 09:01 装车：车辆仍在 003，不进在途、不安排续跑
    order = {'cutoff': '2026-11-20T08:35:00+08:00', 'signoffs': [
        paper('M-01', 'accepted', 3, '2026-11-20T08:20:00+08:00', 'a')]}
    out = build_handover(shift, snap, order)
    m02 = next(row for row in out['legs'] if row['leg'] == 'M-02')
    assert m02['status'] == 'pending' and m02['remaining_on_truck'] == 2
    assert out['receivers']['T-02']['bikes'] == 0
    assert out['options']['original'] == [] and out['options']['rerouted'] == []
    assert out['carried_bikes'] == 0 and out['fleet'] == 33


def test_closed_station_holding_is_preserved_in_next_opening(shift, snap):
    order = {'cutoff': '2026-11-20T08:35:00+08:00', 'signoffs': [
        paper('M-01', 'accepted', 3, '2026-11-20T08:20:00+08:00', 'a')]}
    out = build_handover(shift, snap, order)
    # 003 在 08:30-08:45 关站，7 辆车在站内暂存，不能丢
    assert out['opening']['003'] == {'docked': 0, 'held': 7, 'capacity': 0, 'free': 0}


def test_missing_next_shift_has_explicit_domain_error(sources, snap):
    from app.shift import Shift
    raw = json.loads(sources['shift'])
    raw.pop('next_shift', None)
    shift = Shift(raw['name'], instant(raw['start']), instant(raw['end']), raw['stations'],
                  raw['trucks'], raw['opening_road'], raw['min_travel_minutes'])
    order = {'cutoff': CUTOFF, 'signoffs': [
        paper('M-01', 'accepted', 1, '2026-11-20T08:20:00+08:00', 'a')]}
    with pytest.raises(DomainError, match='NEXT_SHIFT_MISSING'):
        build_handover(shift, snap, order)
