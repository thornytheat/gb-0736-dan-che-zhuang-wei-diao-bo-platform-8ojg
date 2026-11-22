from flask import Blueprint, Response, jsonify, request

from app.errors import DomainError
from app.manifest import canonical, digest
from app.reasons import REASONS
from app.web import store
from . import service

bp = Blueprint('handover', __name__)


def order_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainError('BAD_REQUEST')
    return data


@bp.get('/api/handover')
def show():
    handover_id = request.args.get('handover')
    if handover_id is None and store().active_handover() is None:
        return jsonify(id=None, data=None, history=[])
    return jsonify(id=handover_id or store().active_handover(),
                   data=service.show(store(), handover_id),
                   history=store().list_handovers())


@bp.post('/api/handover/preview')
def preview():
    data = order_body()
    if set(data) != {'run', 'cutoff', 'signoffs'}:
        raise DomainError('BAD_REQUEST', fields=['run', 'cutoff', 'signoffs'])
    if not isinstance(data['run'], str) or not data['run']:
        raise DomainError('BAD_REQUEST', field='run')
    result = service.preview(store(), data['run'],
                             {'cutoff': data['cutoff'], 'signoffs': data['signoffs']})
    return jsonify(handover=result)


@bp.post('/api/handover/publish')
def publish():
    data = order_body()
    if set(data) != {'expected', 'cutoff', 'signoffs'}:
        raise DomainError('BAD_REQUEST', fields=['expected', 'cutoff', 'signoffs'])
    if not isinstance(data['expected'], str) or not data['expected']:
        raise DomainError('BAD_REQUEST', field='expected')
    result = service.publish(store(), data['expected'],
                             {'cutoff': data['cutoff'], 'signoffs': data['signoffs']})
    return jsonify(result)


@bp.get('/api/handover/<handover_id>/evidence.json')
def export(handover_id):
    handover = service.show(store(), handover_id)
    payload = dict(handover, reason_labels=REASONS)
    body = {'handover_sha256': digest(canonical(payload).encode()), 'handover': payload}
    return Response(canonical(body) + '\n', mimetype='application/json',
                    headers={'Content-Disposition':
                             f'attachment; filename="handover-{handover_id[:12]}.json"'})
