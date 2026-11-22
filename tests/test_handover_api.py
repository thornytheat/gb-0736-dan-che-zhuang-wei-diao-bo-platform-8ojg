def paper(leg, kind='accepted', count=3, at='2026-11-20T08:20:00+08:00', text='东门纸质签收'):
    return {'leg': leg, 'kind': kind, 'count': count, 'at': at, 'text': text}


def order(run, cutoff='2026-11-20T09:30:00+08:00'):
    return {'expected': run, 'cutoff': cutoff, 'signoffs': [
        paper('M-01'), paper('M-02', count=1, at='2026-11-20T09:12:00+08:00', text='广场纸质签收 1 辆')]}


def preview_order(run):
    body = order(run)
    return {'run': body['expected'], 'cutoff': body['cutoff'], 'signoffs': body['signoffs']}


def test_preview_returns_execution_sheet_without_persisting(client, run_id, app):
    res = client.post('/api/handover/preview', json=preview_order(run_id))
    assert res.status_code == 200
    data = res.json['handover']
    assert [row['leg'] for row in data['legs']] == ['M-01', 'M-02']
    assert data['fleet'] == 33 and data['expires_at'] == '2026-11-20T10:00:00+08:00'
    with app.extensions['shift_store'].connect() as conn:
        assert conn.execute('SELECT count(*) FROM handovers').fetchone()[0] == 0


def test_publish_persists_sheet_and_all_raw_papers(client, run_id, app):
    res = client.post('/api/handover/publish', json=order(run_id))
    assert res.status_code == 200 and res.json['created'] is True
    hid = res.json['id']
    db = app.extensions['shift_store']
    assert db.active_handover() == hid
    with db.connect() as conn:
        rows = conn.execute('SELECT leg, bikes, raw, duplicate FROM handover_papers ORDER BY seq').fetchall()
    assert rows == [('M-01', 3, '东门纸质签收', 0), ('M-02', 1, '广场纸质签收 1 辆', 0)]
    fetched = client.get('/api/handover').json
    assert fetched['id'] == hid and fetched['history'][0]['cutoff'] == '2026-11-20T01:30:00Z'


def test_retry_same_sheet_is_idempotent_and_keeps_single_archive(client, run_id, app):
    first = client.post('/api/handover/publish', json=order(run_id))
    second = client.post('/api/handover/publish', json=order(run_id))
    assert first.json['id'] == second.json['id']
    assert second.json['created'] is False and second.json['changed'] is False
    db = app.extensions['shift_store']
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM handovers').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM handover_papers').fetchone()[0] == 2
    # 同一张单据的重复上报进入留档并标记 duplicate；执行口径不变（已卸仍为 3 辆）
    dup = order(run_id)
    dup['signoffs'].insert(1, paper('M-01'))
    third = client.post('/api/handover/publish', json=dup)
    assert third.json['handover']['legs'][0]['unloaded'] == 3
    assert third.json['handover']['signoffs'][0]['duplicate'] is False
    assert third.json['handover']['signoffs'][1]['duplicate'] is True
    with db.connect() as conn:
        # 首张原件 1 行 + 含重复件的新交接单 2 行，三张原件全部留档未误删
        assert conn.execute("SELECT count(*) FROM handover_papers WHERE leg = 'M-01'").fetchone()[0] == 3
        assert conn.execute(
            "SELECT count(*) FROM handover_papers WHERE duplicate = 1").fetchone()[0] == 1


def test_failed_publish_keeps_old_sheet_old_plan_and_inventory(client, run_id, app):
    client.post('/api/handover/publish', json=order(run_id))
    db = app.extensions['shift_store']
    with db.connect() as conn:
        conn.execute("CREATE TRIGGER stop_h BEFORE INSERT ON handover_papers "
                     "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    bad = order(run_id, cutoff='2026-11-20T09:45:00+08:00')
    res = client.post('/api/handover/publish', json=bad)
    assert res.status_code == 503 and res.json['code'] == 'STORE_UNAVAILABLE'
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM handovers').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM handover_papers').fetchone()[0] == 2
    # 旧行程与旧库存仍可查
    legs = client.get('/api/dispatch').json['data']['legs']
    assert [leg['id'] for leg in legs] == ['M-01', 'M-02']
    assert client.get('/health').json['run'] == run_id


def test_stale_handover_rejected_after_run_changes(client, run_id):
    newer = client.post('/api/dispatch/publish', json={'expected': run_id, 'legs': []}).json['id']
    stale = client.post('/api/handover/publish', json=order(run_id))
    assert stale.status_code == 409 and stale.json['code'] == 'STALE_RUN'
    assert stale.json['details']['current'] == newer


def test_validation_errors_do_not_touch_storage(client, run_id, app):
    bad = order(run_id)
    bad['signoffs'][0]['leg'] = 'M-99'
    res = client.post('/api/handover/publish', json=bad)
    assert res.status_code == 422 and res.json['code'] == 'SIGNOFF_UNKNOWN_LEG'
    with app.extensions['shift_store'].connect() as conn:
        assert conn.execute('SELECT count(*) FROM handovers').fetchone()[0] == 0


def test_missing_handover_and_bad_shape_are_recoverable(client, run_id):
    missing = client.get('/api/handover?handover=does-not-exist')
    assert missing.status_code == 404 and missing.json['code'] == 'HANDOVER_NOT_FOUND'
    bad_shape = client.post('/api/handover/publish', json={'expected': run_id})
    assert bad_shape.status_code == 422 and bad_shape.json['code'] == 'BAD_REQUEST'


def test_handover_history_lists_every_published_sheet(client, run_id):
    first = client.post('/api/handover/publish', json=order(run_id)).json['id']
    second = client.post('/api/handover/publish',
                         json=order(run_id, '2026-11-20T09:50:00+08:00')).json['id']
    assert first != second
    history = client.get('/api/handover').json['history']
    assert [row['id'] for row in history] == [second, first]
    assert [row['active'] for row in history] == [True, False]
    old = client.get(f'/api/handover?handover={first}')
    assert old.status_code == 200 and old.json['id'] == first


def test_evidence_download_keeps_raw_signoff_and_matches_digest(client, run_id):
    from app.manifest import canonical, digest
    hid = client.post('/api/handover/publish', json=order(run_id)).json['id']
    res = client.get(f'/api/handover/{hid}/evidence.json')
    assert res.status_code == 200
    assert res.headers['Content-Disposition'].startswith('attachment;')
    body = res.json
    assert body['handover_sha256'] == digest(canonical(body['handover']).encode())
    texts = [paper['text'] for paper in body['handover']['signoffs']]
    assert '东门纸质签收' in texts and '广场纸质签收 1 辆' in texts
