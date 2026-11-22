from datetime import datetime, timezone

from app.clock import stamp
from app.errors import DomainError
from app.manifest import canonical, digest
from app.shift import load_shift
from .domain import build_handover


def handover_id(run_id, order):
    """交接单编号由所在班次、交接时点、执行结果与规范化签收单共同决定。

    行程执行结果纳入指纹后，同一纸面单据在不同班次重算也不会撞号，
    避免跨班幂等误判。
    """
    identity = {'run': run_id, 'cutoff': order['cutoff'],
                'legs': order['legs'], 'receivers': order['receivers'],
                'signoffs': order['signoffs']}
    return digest(canonical(identity).encode())


def preview(store, run_id, order):
    snapshot = store.load(run_id)
    shift = load_shift(store.sources(run_id)['shift'])
    return build_handover(shift, snapshot, order)


def publish(store, run_id, order):
    # 与班次发布同一口径：先校验 expected 是否仍是当前班次，
    # 旧页面在新班次发布后再交单必须被拒绝，避免覆盖新行程与新库存
    if store.active() != run_id:
        raise DomainError('STALE_RUN', 409, current=store.active())
    snapshot = store.load(run_id)
    sources = store.sources(run_id)
    shift = load_shift(sources['shift'])
    handover = build_handover(shift, snapshot, order)
    hid = handover_id(run_id, handover)
    # 同一交接单重复发布走幂等：命中当前生效交接单时直接返回，不重复留档、不重复计数
    if store.active_handover() == hid:
        return {'id': hid, 'created': False, 'changed': False, 'handover': handover}
    published_at = stamp(datetime.now(timezone.utc))
    store.publish_handover(hid, run_id, published_at, handover, handover['signoffs'], run_id)
    return {'id': hid, 'created': True, 'changed': True, 'handover': handover}


def show(store, handover_id):
    return store.load_handover(handover_id)
