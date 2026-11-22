import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import DomainError
from .manifest import canonical

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    run_id TEXT NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    raw BLOB NOT NULL,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS active (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    run_id TEXT NOT NULL REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS handovers (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    published_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_papers (
    handover_id TEXT NOT NULL REFERENCES handovers(id),
    leg TEXT NOT NULL,
    kind TEXT NOT NULL,
    bikes INTEGER NOT NULL,
    at TEXT NOT NULL,
    raw TEXT NOT NULL,
    duplicate INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (handover_id, seq)
);
CREATE TABLE IF NOT EXISTS active_handover (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    handover_id TEXT NOT NULL REFERENCES handovers(id)
);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def active(self):
        with self.connect() as db:
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
        return row[0] if row else None

    def load(self, run_id=None):
        with self.connect() as db:
            if run_id is None:
                row = db.execute('SELECT payload FROM runs JOIN active ON runs.id = active.run_id').fetchone()
            else:
                row = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
        if row is None:
            raise DomainError('RUN_NOT_FOUND', 404)
        return json.loads(row[0])

    def sources(self, run_id):
        with self.connect() as db:
            return dict(db.execute('SELECT name, raw FROM sources WHERE run_id = ?', (run_id,)))

    def history(self):
        sql = '''SELECT runs.id, generated_at, json_extract(payload, '$.shift.name'),
                        json_extract(payload, '$.manifest.inputs.events.sha256'),
                        json_extract(payload, '$.dispatch.comparison.delta'), active.run_id IS NOT NULL
                 FROM runs LEFT JOIN active ON runs.id = active.run_id
                 ORDER BY generated_at DESC, runs.id'''
        keys = ('id', 'generated_at', 'shift', 'source_sha256', 'delta', 'active')
        with self.connect() as db:
            return [dict(zip(keys, row)) for row in db.execute(sql)]

    def publish(self, snapshot, sources, expected):
        manifest = snapshot['manifest']
        run_id = manifest['id']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            active = row[0] if row else None
            existing = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
            if existing is not None:
                stored = json.loads(existing[0])
                comparable = dict(snapshot, manifest=dict(manifest))
                for key in ('generated_at', 'elapsed_ms'):
                    comparable['manifest'][key] = stored['manifest'][key]
                if stored != comparable:
                    raise DomainError('REPLAY_DRIFT', 409)
                if active == run_id:
                    return False
            if active != expected:
                raise DomainError('STALE_RUN', 409, current=active)
            if existing is None:
                db.execute('INSERT INTO runs VALUES (?, ?, ?)',
                           (run_id, manifest['generated_at'], canonical(snapshot)))
                db.executemany('INSERT INTO sources VALUES (?, ?, ?)',
                               [(run_id, name, raw) for name, raw in sources.items()])
            db.execute('INSERT INTO active VALUES (1, ?) ON CONFLICT(slot) DO UPDATE SET run_id = excluded.run_id',
                       (run_id,))
            return existing is None

    def active_handover(self):
        with self.connect() as db:
            row = db.execute('SELECT handover_id FROM active_handover WHERE slot = 1').fetchone()
        return row[0] if row else None

    def load_handover(self, handover_id=None):
        with self.connect() as db:
            if handover_id is None:
                row = db.execute(
                    'SELECT payload FROM handovers JOIN active_handover ON handovers.id = active_handover.handover_id'
                ).fetchone()
            else:
                row = db.execute('SELECT payload FROM handovers WHERE id = ?', (handover_id,)).fetchone()
        if row is None:
            raise DomainError('HANDOVER_NOT_FOUND', 404)
        return json.loads(row[0])

    def list_handovers(self):
        sql = '''SELECT handovers.id, handovers.run_id, handovers.published_at,
                        json_extract(payload, '$.cutoff'), active_handover.handover_id IS NOT NULL
                 FROM handovers LEFT JOIN active_handover ON handovers.id = active_handover.handover_id
                 ORDER BY handovers.published_at DESC, handovers.id'''
        keys = ('id', 'run_id', 'published_at', 'cutoff', 'active')
        with self.connect() as db:
            return [dict(zip(keys, row)) for row in db.execute(sql)]

    def publish_handover(self, handover_id, run_id, published_at, payload, papers, expected_run):
        """事务内发布交接单；任一步失败都保留旧行程与旧库存。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            active_run = row[0] if row else None
            if active_run != expected_run:
                raise DomainError('STALE_RUN', 409, current=active_run)
            existing = db.execute('SELECT 1 FROM runs WHERE id = ?', (run_id,)).fetchone()
            if existing is None:
                raise DomainError('RUN_NOT_FOUND', 404)
            db.execute('INSERT INTO handovers VALUES (?, ?, ?, ?)',
                       (handover_id, run_id, published_at, canonical(payload)))
            db.executemany(
                'INSERT INTO handover_papers VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                [(handover_id, paper['leg'], paper['kind'], paper['count'], paper['at'],
                  paper['text'], int(paper['duplicate']), seq)
                 for seq, paper in enumerate(papers)])
            db.execute(
                'INSERT INTO active_handover VALUES (1, ?) '
                'ON CONFLICT(slot) DO UPDATE SET handover_id = excluded.handover_id',
                (handover_id,))
