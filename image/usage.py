"""Sampled Mihomo accounting: one difference calculation, four persistent tables."""
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
import os
import pathlib
import re
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE attribution (
 id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE,
 source TEXT NOT NULL, protocol TEXT NOT NULL, host TEXT NOT NULL,
 destination TEXT NOT NULL, port INTEGER NOT NULL, inbound TEXT NOT NULL,
 outbound TEXT NOT NULL, kind TEXT NOT NULL, chain TEXT NOT NULL,
 rule TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE samples (
 id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE, core TEXT NOT NULL,
 time_ms INTEGER NOT NULL, start_ms INTEGER NOT NULL, mono_ns INTEGER NOT NULL,
 upload_total INTEGER NOT NULL, download_total INTEGER NOT NULL,
 upload INTEGER, download INTEGER, flags TEXT NOT NULL,
 history_instance TEXT, history_sequence INTEGER, history_start_ms INTEGER
);
CREATE INDEX samples_time ON samples(time_ms);
CREATE TABLE usage (
 sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
 attribution_id INTEGER NOT NULL REFERENCES attribution(id),
 start_ms INTEGER NOT NULL, quality TEXT NOT NULL,
 upload INTEGER NOT NULL CHECK(upload>=0), download INTEGER NOT NULL CHECK(download>=0),
 PRIMARY KEY(sample_id, attribution_id, start_ms, quality)
) WITHOUT ROWID;
CREATE INDEX usage_attribution ON usage(attribution_id);
CREATE TABLE checkpoints (
 key TEXT PRIMARY KEY, upload INTEGER NOT NULL, download INTEGER NOT NULL,
 seen_ms INTEGER NOT NULL, mono_ns INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX checkpoints_seen ON checkpoints(seen_ms);
PRAGMA user_version=2;
"""
GROUPS = {'host': "COALESCE(NULLIF(a.host,''),NULLIF(a.destination,''),'unknown')",
          'outbound': "COALESCE(NULLIF(a.outbound,''),'unknown')",
          'protocol': 'a.protocol', 'source': "COALESCE(NULLIF(a.source,''),'unknown')"}


def integer(value):
    if type(value) is not int or not 0 <= value < 2 ** 63:
        raise ValueError('Invalid traffic counter')
    return value


def text(value, limit=1024):
    return value[:limit] if isinstance(value, str) else ''


def started_ms(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('Invalid connection start')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Connection start has no timezone')
    return int(result.timestamp() * 1000)


def attribution(connection):
    metadata = connection.get('metadata') or {}
    if not isinstance(metadata, dict):
        raise ValueError('Invalid metadata')
    chain = connection.get('chains') or []
    if not isinstance(chain, list) or len(chain) > 64 or not all(isinstance(v, str) for v in chain):
        raise ValueError('Invalid proxy chain')
    chain = [text(v) for v in chain]
    # Chain.Last() in the pinned Mihomo core returns element 0, the actual leaf.
    outbound = chain[0] if chain else ''
    kind = ('direct' if outbound == 'DIRECT' else 'unknown'
            if not outbound or outbound in ('REJECT', 'REJECT-DROP', 'PASS') else 'proxy')
    protocol = metadata.get('network')
    protocol = protocol if protocol in ('tcp', 'udp') else 'unknown'
    try:
        port = int(metadata.get('destinationPort') or 0)
    except (ValueError, TypeError):
        port = 0
    return (text(metadata.get('sourceIP')), protocol, text(metadata.get('host')).lower().rstrip('.'),
            text(metadata.get('destinationIP')), port if 0 <= port <= 65535 else 0,
            text(metadata.get('type')), outbound, kind, json.dumps(chain, ensure_ascii=False, separators=(',', ':')),
            text(connection.get('rule')), text(connection.get('rulePayload'), 4096))


@dataclass
class Checkpoint:
    upload: int
    download: int
    seen_ms: int
    mono_ns: int


class Ledger:
    """Only flush writes. Usage rows and corresponding high-water marks commit together."""

    def __init__(self, path, *, max_bytes=1024 ** 3):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=0.5, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA cache_size=-4096')
        self.db.execute('PRAGMA journal_size_limit=8388608')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            if self.db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
                raise ValueError('Unrecognized usage database')
            self.db.executescript('BEGIN;\n' + SCHEMA + '\nCOMMIT;')
        elif version == 1:
            self.db.executescript('BEGIN; ALTER TABLE samples ADD COLUMN history_instance TEXT; '
                                 'ALTER TABLE samples ADD COLUMN history_sequence INTEGER; '
                                 'ALTER TABLE samples ADD COLUMN history_start_ms INTEGER; '
                                 'PRAGMA user_version=2; COMMIT;')
        elif version != 2:
            raise ValueError('Unsupported usage database version')
        page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
        self.db.execute('PRAGMA max_page_count=' + str(max(128, max_bytes // page_size)))
        self.db.row_factory = sqlite3.Row
        row = self.db.execute('SELECT * FROM samples ORDER BY id DESC LIMIT 1').fetchone()
        self.previous = dict(row) if row else None
        self.next_id = (row['id'] if row else 0) + 1
        self.next_attr = self.db.execute('SELECT COALESCE(max(id),0)+1 FROM attribution').fetchone()[0]
        self.connections = {row['key']: Checkpoint(row['upload'], row['download'], row['seen_ms'], row['mono_ns'])
                            for row in self.db.execute('SELECT * FROM checkpoints ORDER BY seen_ms DESC LIMIT 50000')}
        self.cache = OrderedDict()
        self.attributes, self.samples, self.rows, self.dirty = {}, [], [], {}
        self.reset_checkpoints = False
        self.run_id, self.sequence = uuid.uuid4().hex, 0
        self.last_commit_ms = row['time_ms'] if row else None
        self.committed_history_instance = row['history_instance'] if row else None
        self.failed = False
        self.retention_hours = 48

    @property
    def history_cursor(self):
        return (self.previous or {}).get('history_sequence') or 0

    def checkpoint(self, key):
        value = self.connections.get(key) or self.dirty.get(key)
        if value is None and not self.reset_checkpoints:
            row = self.db.execute('SELECT * FROM checkpoints WHERE key=?', (key,)).fetchone()
            if row:
                value = Checkpoint(row['upload'], row['download'], row['seen_ms'], row['mono_ns'])
        return value

    def close(self):
        self.db.close()

    def dimension(self, values):
        key = json.dumps(values, ensure_ascii=False, separators=(',', ':'))
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        if key in self.attributes:
            return self.attributes[key][0]
        row = self.db.execute('SELECT id FROM attribution WHERE key=?', (key,)).fetchone()
        if row:
            result = row[0]
        else:
            result, self.next_attr = self.next_attr, self.next_attr + 1
            self.attributes[key] = (result, key, *values)
        self.cache[key] = result
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return result

    def ingest(self, snapshot, core, wall_ms, mono_ns):
        upload, download = integer(snapshot.get('uploadTotal')), integer(snapshot.get('downloadTotal'))
        items = snapshot.get('connections')
        if items is None:
            items = []  # Mihomo marshals an empty connection slice as null.
        if not isinstance(items, list) or len(items) > 20000:
            raise ValueError('Invalid connection list')
        previous = self.previous
        initial, changed = previous is None, previous is not None and previous['core'] != core
        flags = set()
        history = snapshot.get('history')
        enabled = isinstance(history, dict) and history.get('enabled') is True
        history_instance = history_sequence = history_start = None
        closed_items = []
        if enabled:
            history_instance = history.get('instance')
            if not isinstance(history_instance, str) or not history_instance or len(history_instance) > 128:
                raise ValueError('Invalid history instance')
            history_sequence = integer(history.get('nextSequence'))
            latest = integer(history.get('latestSequence'))
            closed_items = history.get('records')
            if not isinstance(closed_items, list) or len(closed_items) > 4096 or history_sequence > latest:
                raise ValueError('Invalid history page')
            if previous and previous.get('history_instance') == history_instance and not changed:
                history_start = previous.get('history_start_ms')
                if history_sequence < (previous.get('history_sequence') or 0):
                    raise ValueError('Regressing history cursor')
            else:
                history_start = 0 if changed else (previous['time_ms'] if previous else wall_ms)
                if previous and not changed:
                    flags.add('history_changed')
            if history.get('lost') or history.get('reset'):
                flags.add('history_lost')
            if history_sequence < latest:
                flags.add('history_backlog')
        start = previous['time_ms'] if previous else wall_ms
        elapsed = (mono_ns - previous['mono_ns']) / 1e6 if previous and not changed else None
        gap = bool(previous and (changed or elapsed < 0 or elapsed > 1800))
        if initial:
            flags.add('baseline')
            global_up = global_down = None
        elif changed:
            flags.add('core_changed')
            self.connections.clear()
            self.dirty.clear()
            self.reset_checkpoints = True
            global_up, global_down = upload, download
        else:
            global_up = upload - previous['upload_total'] if upload >= previous['upload_total'] else None
            global_down = download - previous['download_total'] if download >= previous['download_total'] else None
            if global_up is None or global_down is None:
                flags.add('global_reset')
        clock_jump = bool(previous and (wall_ms < start or
                          (elapsed is not None and abs((wall_ms - start) - elapsed) > 2000)))
        if clock_jump:
            flags.add('clock_jump')
        if gap:
            flags.add('gap')
        merged, unique = defaultdict(lambda: [0, 0]), {}
        for item, is_closed in [(item, False) for item in items] + [(item, True) for item in closed_items]:
            try:
                if not isinstance(item, dict):
                    raise ValueError('Invalid connection')
                metadata = item.get('metadata') or {}
                if not isinstance(metadata, dict):
                    raise ValueError('Invalid metadata')
                if item.get('counted') is False or (item.get('counted') is None and text(metadata.get('type')).lower() == 'inner'):
                    continue
                if is_closed and type(item.get('counted')) is not bool:
                    raise ValueError('History record has no accounting scope')
                identifier, stamp = item.get('id'), item.get('start')
                if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
                    raise ValueError('Invalid connection ID')
                born = started_ms(stamp)
                up, down = integer(item.get('upload')), integer(item.get('download'))
                key = json.dumps((core, identifier, stamp), separators=(',', ':'))
                values = attribution(item)
                first_close = is_closed and integer(item.get('revision')) == 1
                if key in unique:
                    old = unique[key]
                    if not is_closed and not old[4]:
                        flags.add('duplicate_connection')
                        if old[3] != values:
                            raise ValueError('Conflicting connection metadata')
                    up, down = max(up, old[0]), max(down, old[1])
                    is_closed, first_close = is_closed or old[4], first_close or old[5]
                unique[key] = (up, down, born, values, is_closed, first_close)
            except (ValueError, TypeError, OverflowError):
                flags.add('invalid_connection')
        for key, (up, down, born, values, is_closed, first_close) in unique.items():
            old = self.checkpoint(key)
            if old:
                du, dd = max(0, up - old.upload), max(0, down - old.download)
                if up < old.upload or down < old.download:
                    flags.add('connection_regression')
                since = old.seen_ms
                recovered = gap or clock_jump or mono_ns - old.mono_ns > 1800 * 1000000
                high_up, high_down = max(up, old.upload), max(down, old.download)
            else:
                # Unknown old connections may be evicted tombstones; never recount their lifetime.
                threshold = max(history_start or 0, wall_ms - self.retention_hours * 3600000)
                # Snapshot enumeration and its collector timestamp are not atomic.
                # A new active flow can start just before the previous timestamp
                # without appearing in that snapshot. Coverage and retained disk
                # checkpoints let us include it without replaying old lifetimes.
                known_lifetime = enabled and born >= threshold and (not is_closed or first_close)
                count = not initial and (changed or (not clock_jump and (known_lifetime or (not is_closed and born >= start))))
                du, dd = (up, down) if count else (0, 0)
                since = (born if is_closed or known_lifetime else start) if count else wall_ms
                recovered = (gap or clock_jump or (known_lifetime and born < start)
                             or (is_closed and wall_ms - since > 1800))
                high_up, high_down = up, down
                if not initial and not changed and not count:
                    flags.add('late_connection_baseline')
            checkpoint = Checkpoint(high_up, high_down, wall_ms, mono_ns)
            self.connections[key] = self.dirty[key] = checkpoint
            if du or dd:
                dimension = self.dimension(values)
                quality = ('closed-recovered' if recovered else 'closed') if is_closed else ('recovered' if recovered else 'normal')
                target = merged[(dimension, since, quality)]
                target[0] += du
                target[1] += dd
        self.sequence += 1
        sample = dict(id=self.next_id, token=f'{self.run_id}:{self.sequence}', core=core,
                      time_ms=wall_ms, start_ms=start, mono_ns=mono_ns, upload_total=upload,
                      download_total=download, upload=global_up, download=global_down,
                      flags=json.dumps(sorted(flags)), history_instance=history_instance,
                      history_sequence=history_sequence, history_start_ms=history_start)
        self.next_id += 1
        self.samples.append(sample)
        self.rows.extend((sample['id'], dimension, since, quality, up, down)
                         for (dimension, since, quality), (up, down) in merged.items())
        self.previous = sample
        if len(self.connections) > 50000:
            # Bound tombstone memory under unusually high connection churn. Unknown old
            # connections still establish a baseline if they reappear after eviction.
            active = set(unique)
            recent = sorted((key for key in self.connections if key not in active),
                            key=lambda key: self.connections[key].seen_ms, reverse=True)
            keep = active | set(recent[:max(0, 40000 - len(active))])
            self.connections = {key: value for key, value in self.connections.items() if key in keep}
        return sorted(flags)

    def flush(self):
        if self.failed:
            raise RuntimeError('Ledger must reopen after a failed transaction')
        if not self.samples:
            return
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.executemany('INSERT INTO attribution VALUES (' + ','.join('?' * 13) + ')', self.attributes.values())
            fields = ('id', 'token', 'core', 'time_ms', 'start_ms', 'mono_ns', 'upload_total',
                      'download_total', 'upload', 'download', 'flags', 'history_instance', 'history_sequence', 'history_start_ms')
            self.db.executemany('INSERT INTO samples VALUES (' + ','.join('?' * len(fields)) + ')',
                                [tuple(sample[name] for name in fields) for sample in self.samples])
            self.db.executemany('INSERT INTO usage VALUES (?,?,?,?,?,?)', self.rows)
            if self.reset_checkpoints:
                self.db.execute('DELETE FROM checkpoints')
            self.db.executemany('INSERT INTO checkpoints VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET '
                                'upload=excluded.upload,download=excluded.download,'
                                'seen_ms=excluded.seen_ms,mono_ns=excluded.mono_ns',
                                [(key, value.upload, value.download, value.seen_ms, value.mono_ns)
                                 for key, value in self.dirty.items()])
            self.db.execute('COMMIT')
        except Exception:
            self.failed = True  # Do not replay an ambiguously committed batch in memory.
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        self.last_commit_ms = self.samples[-1]['time_ms']
        self.committed_history_instance = self.samples[-1]['history_instance']
        self.attributes.clear()
        self.samples.clear()
        self.rows.clear()
        self.dirty.clear()
        self.reset_checkpoints = False

    def prune(self, now_ms, retention_hours=48):
        if self.samples:
            raise RuntimeError('Flush before retention maintenance')
        cutoff = now_ms - retention_hours * 3600000
        # Small transactions; retain the latest sample as the global checkpoint.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM samples WHERE id IN (SELECT id FROM samples WHERE time_ms<? '
                            'AND id<>(SELECT max(id) FROM samples) ORDER BY id LIMIT 1000)', (cutoff,))
            self.db.execute('DELETE FROM checkpoints WHERE seen_ms<?', (cutoff,))
            self.db.execute('DELETE FROM attribution WHERE id IN (SELECT a.id FROM attribution a '
                            'WHERE NOT EXISTS(SELECT 1 FROM usage u WHERE u.attribution_id=a.id) LIMIT 1000)')
        self.cache.clear()
        self.connections = {key: value for key, value in self.connections.items()
                            if value.seen_ms >= cutoff}
        self.retention_hours = retention_hours


def duration(value):
    match = re.fullmatch(r'([1-9][0-9]*)([smhd])', value)
    if not match:
        raise ValueError('Use a duration such as 15m, 1h or 2d')
    seconds = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
    if seconds > 366 * 86400:
        raise ValueError('Query duration exceeds 366 days')
    return seconds


def report(path, *, since='1h', group_by='host', proxy_only=False, limit=20, bucket_seconds=60, now_ms=None):
    if group_by not in (*GROUPS, 'time') or not 1 <= limit <= 500 or bucket_seconds not in (1, 60, 3600):
        raise ValueError('Invalid usage query')
    end = int(time.time() * 1000) if now_ms is None else now_ms
    cutoff = end - duration(since) * 1000
    uri = pathlib.Path(path).resolve().as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True, timeout=0.5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        # Every query uses one read snapshot, released before formatting/output.
        db.execute('BEGIN')
        totals = dict(db.execute('SELECT count(*) AS samples,min(time_ms) AS first_sample_ms,'
                  'max(time_ms) AS last_sample_ms,COALESCE(sum(upload),0) AS upload,'
                  'COALESCE(sum(download),0) AS download,'
                  'sum(CASE WHEN upload IS NULL OR download IS NULL THEN 1 ELSE 0 END) AS unknown_intervals,'
                  'sum(CASE WHEN flags<>\'[]\' THEN 1 ELSE 0 END) AS flagged_samples '
                  'FROM samples WHERE time_ms>? AND time_ms<=? AND start_ms>=?', (cutoff, end, cutoff)).fetchone())
        filters = 's.time_ms>? AND s.time_ms<=?' + (" AND a.kind='proxy'" if proxy_only else '')
        base = ' FROM usage u JOIN samples s ON s.id=u.sample_id JOIN attribution a ON a.id=u.attribution_id WHERE ' + filters
        observed = dict(db.execute('SELECT COALESCE(sum(CASE WHEN u.start_ms>=? THEN u.upload ELSE 0 END),0) AS upload,'
                      'COALESCE(sum(CASE WHEN u.start_ms>=? THEN u.download ELSE 0 END),0) AS download,'
                      "COALESCE(sum(CASE WHEN u.start_ms>=? AND u.quality IN ('recovered','closed-recovered') THEN u.upload ELSE 0 END),0) AS recovered_upload,"
                      "COALESCE(sum(CASE WHEN u.start_ms>=? AND u.quality IN ('recovered','closed-recovered') THEN u.download ELSE 0 END),0) AS recovered_download,"
                      "COALESCE(sum(CASE WHEN u.start_ms>=? AND u.quality IN ('closed','closed-recovered') THEN u.upload ELSE 0 END),0) AS closed_upload,"
                      "COALESCE(sum(CASE WHEN u.start_ms>=? AND u.quality IN ('closed','closed-recovered') THEN u.download ELSE 0 END),0) AS closed_download,"
                      'COALESCE(sum(CASE WHEN u.start_ms<? THEN u.upload+u.download ELSE 0 END),0) AS boundary_bytes'
                      + base, (cutoff,) * 7 + (cutoff, end)).fetchone())
        expression = f'(s.time_ms/{bucket_seconds * 1000})*{bucket_seconds * 1000}' if group_by == 'time' else GROUPS[group_by]
        selected = base + ' AND u.start_ms>=?' + (" AND u.quality IN ('normal','closed')" if group_by == 'time' else '')
        order = 'label DESC' if group_by == 'time' else '(sum(u.upload)+sum(u.download)) DESC,label'
        groups = [dict(row) for row in db.execute('SELECT ' + expression + ' AS label,sum(u.upload) AS upload,'
                  'sum(u.download) AS download,'
                  "sum(CASE WHEN u.quality IN ('recovered','closed-recovered') THEN u.upload+u.download ELSE 0 END) AS recovered_bytes"
                  + selected + ' GROUP BY label ORDER BY ' + order + ' LIMIT ?', (cutoff, end, cutoff, limit))]
        return {'since': since, 'group_by': group_by, 'proxy_only': proxy_only, 'global': totals,
                'observed': observed, 'groups': groups,
                'difference': None if proxy_only else {'upload': totals['upload'] - observed['upload'],
                                                       'download': totals['download'] - observed['download']},
                'notes': ['Global includes DIRECT; never add global and observed.',
                          'Short-lived connections and closing tails may lack attribution.',
                          'Boundary-overlapping bytes are excluded and reported separately.',
                          'Time groups exclude recovered intervals; see recovered totals.']}
    finally:
        db.close()


def format_bytes(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def print_report(value):
    global_, observed = value['global'], value['observed']
    print(f"Window: {value['since']}; committed samples: {global_['samples']}; group: {value['group_by']}")
    print(f"Last sample (UTC Unix ms): {global_['last_sample_ms']}; "
          f"unknown intervals: {global_['unknown_intervals'] or 0}; flagged samples: {global_['flagged_samples'] or 0}")
    print('Mihomo global (including DIRECT): upload ' + format_bytes(global_['upload'])
          + '; download ' + format_bytes(global_['download']))
    print(('Observed proxy' if value['proxy_only'] else 'Observed attributed') + ': upload '
          + format_bytes(observed['upload']) + '; download ' + format_bytes(observed['download']))
    if value['difference']:
        print('Global minus observed: upload ' + format_bytes(value['difference']['upload'])
              + '; download ' + format_bytes(value['difference']['download']) + ' (not extra traffic)')
    print('UPLOAD         DOWNLOAD       DESTINATION / GROUP')
    for row in value['groups']:
        label = (datetime.fromtimestamp(row['label'] / 1000).astimezone().isoformat()
                 if value['group_by'] == 'time' else row['label'])
        print(f"{format_bytes(row['upload']):<14} {format_bytes(row['download']):<14} {label}")
    print('Recovered interval bytes: ' + format_bytes(observed['recovered_upload'] + observed['recovered_download'])
          + '; excluded boundary bytes: ' + format_bytes(observed['boundary_bytes']))
    print('Reconciled from close summaries: ' + format_bytes(observed['closed_upload'] + observed['closed_download']))
    print('Sampled attribution can miss short connections/tails; global and detail are separate views.')
