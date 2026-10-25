import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'image'))
import diagnostics
import usage
import usage_collect

BASE = 1792800000000
MONO = 1000000000000


def connection(name='one', up=0, down=0, born=-60, host='example.com', protocol='tcp',
               outbound='node', inbound='TProxy'):
    return {'id': name, 'start': datetime.fromtimestamp(BASE / 1000 + born, timezone.utc).isoformat(),
            'upload': up, 'download': down, 'chains': [outbound, 'Main'], 'rule': 'DomainSuffix',
            'rulePayload': 'example.com', 'metadata': {'network': protocol, 'type': inbound,
            'sourceIP': '192.168.1.2', 'destinationIP': '203.0.113.1', 'destinationPort': '443', 'host': host}}


def closed(item, sequence=1, revision=1, counted=True):
    return {**item, 'sequence': sequence, 'revision': revision, 'counted': counted,
            'closedAt': datetime.fromtimestamp(BASE / 1000 + 1, timezone.utc).isoformat()}


def history(records=(), sequence=None, instance='history-a', lost=False):
    sequence = max((r['sequence'] for r in records), default=0) if sequence is None else sequence
    return {'enabled': True, 'instance': instance, 'nextSequence': sequence,
            'latestSequence': sequence, 'records': list(records), 'lost': lost}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = pathlib.Path(self.directory) / 'usage.sqlite3'
        self.ledger = usage.Ledger(self.path)
        self.addCleanup(lambda: self.ledger.close())

    def sample(self, second, items=(), up=0, down=0, core='core-a', wall=None, history=None):
        snapshot = {'connections': list(items), 'uploadTotal': up, 'downloadTotal': down}
        if history is not None:
            snapshot['history'] = history
        return self.ledger.ingest(snapshot,
                                 core, BASE + int(second * 1000) if wall is None else wall,
                                 MONO + int(second * 1000000000))

    def total(self):
        self.ledger.flush()
        return tuple(self.ledger.db.execute('SELECT COALESCE(sum(upload),0),COALESCE(sum(download),0) FROM usage').fetchone())

    def reopen(self):
        self.ledger.close()
        self.ledger = usage.Ledger(self.path)

    def test_four_tables_and_private_database(self):
        names = {row[0] for row in self.ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {'attribution', 'samples', 'usage', 'checkpoints'})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_initial_baseline_repeated_snapshot_and_long_connection(self):
        self.sample(0, [connection(up=1000, down=2000)], 1000, 2000)
        self.sample(1, [connection(up=1400, down=2600)], 1400, 2600)
        self.sample(2, [connection(up=1400, down=2600)], 1400, 2600)
        self.sample(3, [connection(up=1700, down=2800)], 1700, 2800)
        self.assertEqual(self.total(), (700, 800))
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM usage').fetchone()[0], 2)

    def test_new_connections_first_bytes_merge_and_proxy_groups_do_not_multiply(self):
        self.sample(0)
        self.sample(1, [connection('a', 100, 200, born=0.1), connection('b', 300, 400, born=0.2)], 400, 600)
        self.assertEqual(self.total(), (400, 600))
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM usage').fetchone()[0], 1)
        dimension = dict(self.ledger.db.execute('SELECT * FROM attribution').fetchone())
        self.assertEqual(dimension['outbound'], 'node')
        self.assertEqual(json.loads(dimension['chain']), ['node', 'Main'])

    def test_duplicate_connection_entries_count_once(self):
        self.sample(0)
        self.sample(1, [connection(up=100, born=0.1), connection(up=100, born=0.1),
                        connection(up=120, born=0.1)], 120)
        self.assertEqual(self.total(), (120, 0))

    def test_inner_trackers_excluded_and_direct_separated(self):
        self.sample(0)
        self.sample(1, [connection('inner', 1000, 1000, born=0.1, inbound='Inner'),
                        connection('direct', 50, 60, born=0.1, outbound='DIRECT'),
                        connection('proxy', 10, 20, born=0.1)], 60, 80)
        self.assertEqual(self.total(), (60, 80))
        value = usage.report(self.path, now_ms=BASE + 1000, proxy_only=True)
        self.assertEqual(value['observed']['upload'], 10)
        self.assertIsNone(value['difference'])

    def test_restart_recovers_committed_checkpoints_without_recounting(self):
        self.sample(0, [connection(up=100)], 100)
        self.sample(1, [connection(up=200)], 200)
        self.ledger.flush()
        self.reopen()
        self.sample(2, [connection(up=200)], 200)
        self.sample(3, [connection(up=350)], 350)
        self.assertEqual(self.total(), (250, 0))

    def test_uncommitted_batch_is_recovered_only_once(self):
        self.sample(0, [connection(up=100)], 100)
        self.ledger.flush()
        self.sample(1, [connection(up=200)], 200)
        self.reopen()  # Simulate a process exit before flush.
        self.sample(2, [connection(up=350)], 350)
        self.assertEqual(self.total(), (250, 0))
        self.reopen()
        self.sample(3, [connection(up=350)], 350)
        self.assertEqual(self.total(), (250, 0))

    def test_transaction_failure_rolls_back_usage_and_checkpoint_together(self):
        self.sample(0, [connection(up=100)], 100)
        self.ledger.flush()
        self.ledger.db.execute("CREATE TRIGGER fail BEFORE INSERT ON checkpoints BEGIN SELECT RAISE(ABORT,'test'); END")
        self.sample(1, [connection(up=200)], 200)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.flush()
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
        self.assertEqual(self.ledger.db.execute('SELECT upload FROM checkpoints').fetchone()[0], 100)
        self.ledger.db.execute('DROP TRIGGER fail')
        self.reopen()
        self.sample(2, [connection(up=300)], 300)
        self.assertEqual(self.total(), (200, 0))

    def test_database_full_keeps_previous_batch_and_can_resume_after_budget_restored(self):
        self.sample(0)
        self.ledger.flush()
        pages = self.ledger.db.execute('PRAGMA page_count').fetchone()[0]
        self.ledger.db.execute('PRAGMA max_page_count=' + str(pages))
        items = [connection(str(n), up=100, born=0.1, host=f'{n}.' + 'x' * 900) for n in range(100)]
        self.sample(1, items, 10000)
        with self.assertRaises(sqlite3.OperationalError):
            self.ledger.flush()
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM samples').fetchone()[0], 1)
        self.assertEqual(self.ledger.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.reopen()
        self.sample(2, items, 10000)
        self.assertEqual(self.total(), (10000, 0))

    def test_core_restart_counts_new_lifetime_once(self):
        self.sample(0, [connection(up=100)], 100)
        self.sample(1, [connection(up=200)], 200)
        flags = self.sample(2, [connection(up=7, born=1.5)], 7, core='core-b')
        self.sample(3, [connection(up=7, born=1.5)], 7, core='core-b')
        self.assertIn('core_changed', flags)
        self.assertEqual(self.total(), (107, 0))
        self.assertEqual(self.ledger.db.execute('SELECT sum(upload) FROM samples').fetchone()[0], 107)

    def test_global_reset_does_not_reset_connection_high_water_marks(self):
        self.sample(0, [connection(up=100)], 100)
        flags = self.sample(1, [connection(up=200)], 10)
        self.sample(2, [connection(up=220)], 30)
        self.assertIn('global_reset', flags)
        self.assertEqual(self.total(), (120, 0))
        self.assertEqual(self.ledger.db.execute('SELECT upload FROM samples WHERE id=2').fetchone()[0], None)

    def test_counter_regression_keeps_high_water_mark(self):
        self.sample(0, [connection(up=1000)], 1000)
        flags = self.sample(1, [connection(up=900)], 1000)
        self.sample(2, [connection(up=1100)], 1100)
        self.assertIn('connection_regression', flags)
        self.assertEqual(self.total(), (100, 0))

    def test_disappearance_and_return_are_recovered_interval_not_new_connection(self):
        self.sample(0, [connection(up=100)], 100)
        self.sample(1, [], 150)
        self.sample(2, [connection(up=200)], 200)
        self.assertEqual(self.total(), (100, 0))
        row = dict(self.ledger.db.execute('SELECT * FROM usage').fetchone())
        self.assertEqual((row['quality'], row['start_ms']), ('recovered', BASE))

    def test_gap_recovery_never_appears_as_one_second_time_peak(self):
        self.sample(0, [connection(up=100)], 100)
        self.sample(30, [connection(up=10000)], 10000)
        self.ledger.flush()
        value = usage.report(self.path, group_by='time', bucket_seconds=1, now_ms=BASE + 30000)
        self.assertEqual(value['groups'], [])
        self.assertEqual(value['observed']['recovered_upload'], 9900)

    def test_unknown_old_connection_is_not_recounted_after_tombstone_expiry(self):
        self.sample(0, [connection(up=100)], 100)
        self.sample(3660, [], 200)
        self.ledger.flush()
        self.ledger.prune(BASE + 3660000, retention_hours=1)
        flags = self.sample(3661, [connection(up=10000)], 200)
        self.sample(3662, [connection(up=10010)], 210)
        self.assertIn('late_connection_baseline', flags)
        self.assertEqual(self.total(), (10, 0))

    def test_metadata_changes_do_not_rewrite_historical_attribution(self):
        self.sample(0, [connection()], 0)
        self.sample(1, [connection(up=100, host='old.example')], 100)
        self.sample(2, [connection(up=200, host='new.example', outbound='DIRECT')], 200)
        self.assertEqual(self.total(), (200, 0))
        value = usage.report(self.path, now_ms=BASE + 2000)
        self.assertEqual({row['label']: row['upload'] for row in value['groups']},
                         {'old.example': 100, 'new.example': 100})

    def test_invalid_counters_do_not_advance_checkpoint_or_create_negative_usage(self):
        self.sample(0, [connection(up=100)], 100)
        flags = self.sample(1, [connection(up=-100)], 100)
        self.assertIn('invalid_connection', flags)
        with self.assertRaises(ValueError):
            self.sample(2, up=True)
        self.sample(3, [connection(up=300)], 300)
        self.assertEqual(self.total(), (200, 0))

    def test_empty_mihomo_connections_can_be_null(self):
        self.ledger.ingest({'connections': None, 'uploadTotal': 0, 'downloadTotal': 0}, 'a', BASE, MONO)
        self.assertEqual(self.total(), (0, 0))

    def test_clock_rollback_and_duplicate_wall_times_do_not_overwrite_samples(self):
        self.sample(0, [connection(up=100)], 100)
        flags = self.sample(1, [connection(up=200)], 200, wall=BASE - 20000)
        self.sample(2, [connection(up=300)], 300, wall=BASE - 20000)
        self.assertIn('clock_jump', flags)
        self.assertEqual(self.total(), (200, 0))
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM samples').fetchone()[0], 3)

    def test_closed_short_connections_are_in_global_but_not_fabricated_detail(self):
        self.sample(0)
        self.sample(1, [], 500, 1000)
        self.ledger.flush()
        value = usage.report(self.path, now_ms=BASE + 1000)
        self.assertEqual((value['global']['upload'], value['global']['download']), (500, 1000))
        self.assertEqual(value['observed']['upload'], 0)
        self.assertEqual(value['difference']['download'], 1000)

    def test_query_boundary_does_not_assign_whole_gap_to_requested_period(self):
        self.sample(0, [connection(up=100)], 100)
        self.sample(30, [connection(up=1000)], 1000)
        self.ledger.flush()
        value = usage.report(self.path, since='10s', now_ms=BASE + 30000)
        self.assertEqual(value['observed']['upload'], 0)
        self.assertEqual(value['observed']['boundary_bytes'], 900)

    def test_retention_cascades_detail_but_preserves_latest_global_checkpoint(self):
        self.sample(0)
        self.sample(1, [connection(up=100, born=0.1)], 100)
        self.sample(7200, [], 100)
        self.ledger.flush()
        self.ledger.prune(BASE + 7200000, retention_hours=1)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM samples').fetchone()[0], 1)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM attribution').fetchone()[0], 0)

    def test_tcp_udp_and_direct_filters_do_not_overlap(self):
        self.sample(0)
        self.sample(1, [connection('tcp', 100, born=0.1), connection('udp', 200, born=0.2, protocol='udp'),
                        connection('direct', 300, born=0.3, outbound='DIRECT')], 600)
        self.ledger.flush()
        value = usage.report(self.path, group_by='protocol', proxy_only=True, now_ms=BASE + 1000)
        self.assertEqual({row['label']: row['upload'] for row in value['groups']}, {'tcp': 100, 'udp': 200})

    def test_cli_usage_reads_committed_data_without_loading_config_or_touching_network(self):
        self.sample(0)
        self.sample(1, [connection(up=100, born=0.1)], 100)
        self.ledger.flush()
        with patch.object(diagnostics, 'DATA', pathlib.Path(self.directory)), \
             patch.object(diagnostics, 'load_config', side_effect=AssertionError('No credentials needed')), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(diagnostics.main(['usage', '--since', '1h', '--json']), 0)
        self.assertIn('observed', json.loads(output.getvalue()))

    def test_query_arguments_are_bounded_and_missing_database_does_not_create_file(self):
        for window in ('0s', '-1h', 'hello', '367d', '1h;DROP TABLE samples'):
            with self.assertRaises(ValueError):
                usage.report(self.path, since=window)
        missing = self.path.with_name('missing.sqlite3')
        with self.assertRaises(sqlite3.OperationalError):
            usage.report(missing)
        self.assertFalse(missing.exists())

    def test_entirely_missed_short_connection_is_reconciled_from_history(self):
        self.sample(0, history=history())
        event = closed(connection(up=1000, down=2000, born=0.1))
        self.sample(1, up=1000, down=2000, history=history([event]))
        self.assertEqual(self.total(), (1000, 2000))
        value = usage.report(self.path, now_ms=BASE + 1000)
        self.assertEqual(value['difference'], {'upload': 0, 'download': 0})
        self.assertEqual(value['observed']['closed_download'], 2000)

    def test_active_plus_closed_same_uuid_and_replays_count_once(self):
        self.sample(0, [connection(up=100)], 100, history=history())
        self.sample(1, [connection(up=200)], 200, history=history())
        event = closed(connection(up=500))
        self.sample(2, [connection(up=500)], 500, history=history([event]))
        self.sample(3, up=500, history=history([event]))
        self.assertEqual(self.total(), (400, 0))

    def test_new_active_connection_between_snapshot_and_timestamp_is_not_baselined(self):
        self.sample(0, history=history())
        # The previous response was timestamped 4 ms after this connection
        # started, but its active-map enumeration had already passed it.
        self.sample(1, up=100, down=200, history=history())
        item = connection(up=150, down=300, born=0.996)
        flags = self.sample(2, [item], 150, 300, history=history())
        self.assertNotIn('late_connection_baseline', flags)
        self.assertEqual(self.total(), (150, 300))
        row = dict(self.ledger.db.execute('SELECT * FROM usage').fetchone())
        self.assertEqual(row['quality'], 'recovered')
        self.assertEqual(row['start_ms'], BASE + 996)
        self.reopen()
        event = closed(connection(up=180, down=360, born=0.996))
        self.sample(3, up=180, down=360, history=history([event]))
        self.sample(4, up=180, down=360, history=history([event]))
        self.assertEqual(self.total(), (180, 360))

    def test_unknown_active_connection_before_history_coverage_keeps_its_baseline(self):
        self.sample(0, history=history())
        flags = self.sample(1, [connection(up=100, born=-60)], 100, history=history())
        self.assertIn('late_connection_baseline', flags)
        self.sample(2, [connection(up=150, born=-60)], 150, history=history())
        self.assertEqual(self.total(), (50, 0))

    def test_expired_active_checkpoint_is_not_recounted_with_history_enabled(self):
        self.sample(0, [connection(up=100)], 100, history=history())
        self.sample(3660, up=200, history=history())
        self.ledger.flush()
        self.ledger.prune(BASE + 3660000, retention_hours=1)
        flags = self.sample(3661, [connection(up=10000)], 200, history=history())
        self.assertIn('late_connection_baseline', flags)
        self.sample(3662, [connection(up=10010)], 210, history=history())
        self.assertEqual(self.total(), (10, 0))

    def test_late_revision_adds_only_the_remaining_delta(self):
        self.sample(0, history=history())
        self.sample(1, up=100, history=history([closed(connection(up=100, born=0.1))]))
        self.sample(2, up=150, history=history([closed(connection(up=150, born=0.1), 2, 2)]))
        self.sample(3, up=150, history=history([closed(connection(up=150, born=0.1), 2, 2)]))
        self.assertEqual(self.total(), (150, 0))

    def test_first_and_late_revision_in_one_page_merge_before_accounting(self):
        self.sample(0, history=history())
        events = [closed(connection(up=150, born=0.1), 2, 2), closed(connection(up=100, born=0.1))]
        self.sample(1, up=150, history=history(events))
        self.assertEqual(self.total(), (150, 0))

    def test_internal_history_records_do_not_leak_into_totals(self):
        self.sample(0, history=history())
        self.sample(1, history=history([closed(connection(up=1000, born=0.1), counted=False)]))
        self.assertEqual(self.total(), (0, 0))

    def test_counted_inner_connections_are_not_filtered_by_a_display_label(self):
        self.sample(0, history=history())
        event = closed(connection(up=100, born=0.1, inbound='Inner'), counted=True)
        self.sample(1, up=100, history=history([event]))
        self.assertEqual(self.total(), (100, 0))

    def test_history_cursor_and_usage_resume_atomically_after_uncommitted_exit(self):
        self.sample(0, history=history())
        self.ledger.flush()
        event = closed(connection(up=100, born=0.1))
        self.sample(1, up=100, history=history([event]))
        self.reopen()
        self.assertEqual(self.ledger.history_cursor, 0)
        self.sample(2, up=100, history=history([event]))
        self.assertEqual(self.total(), (100, 0))
        self.reopen()
        self.assertEqual(self.ledger.history_cursor, 1)
        self.sample(3, up=150, history=history([closed(connection(up=150, born=0.1), 2, 2)]))
        self.assertEqual(self.total(), (150, 0))

    def test_history_cursor_is_not_committed_when_checkpoint_write_fails(self):
        self.sample(0, history=history())
        self.ledger.flush()
        self.ledger.db.execute("CREATE TRIGGER fail BEFORE INSERT ON checkpoints BEGIN SELECT RAISE(ABORT,'test'); END")
        self.sample(1, up=100, history=history([closed(connection(up=100, born=0.1))]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.flush()
        self.assertEqual(self.ledger.db.execute('SELECT history_sequence FROM samples ORDER BY id DESC LIMIT 1').fetchone()[0], 0)

    def test_bootstrap_skips_historical_close_records(self):
        self.sample(0, up=1000, history=history([closed(connection(up=1000, born=-60))]))
        self.sample(1, up=1020, history=history([closed(connection(up=1020, born=-60), 2, 2)]))
        self.assertEqual(self.total(), (20, 0))

    def test_history_loss_and_instance_changes_are_explicit_without_resetting_connections(self):
        self.sample(0, [connection(up=100)], 100, history=history())
        flags = self.sample(1, up=200, history=history([closed(connection(up=200))], lost=True))
        self.assertIn('history_lost', flags)
        flags = self.sample(2, up=250, history=history([closed(connection(up=250), 1, 2)], instance='history-b'))
        self.assertIn('history_changed', flags)
        self.assertEqual(self.total(), (150, 0))

    def test_disk_checkpoint_is_used_after_memory_cache_eviction(self):
        self.sample(0, history=history())
        self.sample(1, up=100, history=history([closed(connection(up=100, born=0.1))]))
        self.ledger.flush()
        self.ledger.connections.clear()
        self.sample(2, up=150, history=history([closed(connection(up=150, born=0.1), 2, 2)]))
        self.assertEqual(self.total(), (150, 0))

    def test_unknown_late_revision_without_checkpoint_establishes_a_baseline(self):
        self.sample(0, history=history())
        flags = self.sample(1, up=100, history=history([closed(connection(up=100, born=0.1), 2, 2)]))
        self.assertIn('late_connection_baseline', flags)
        self.assertEqual(self.total(), (0, 0))

    def test_regressing_history_cursor_does_not_advance_working_state(self):
        self.sample(0, history=history())
        self.sample(1, history=history(sequence=10))
        with self.assertRaises(ValueError):
            self.sample(2, history=history(sequence=5))
        self.assertEqual(self.ledger.history_cursor, 10)

    def test_delayed_first_summary_can_recover_a_lifetime_older_than_previous_poll(self):
        self.sample(0, history=history())
        self.sample(1, history=history())
        self.sample(2, up=100, history=history([closed(connection(up=100, born=0.1))]))
        self.assertEqual(self.total(), (100, 0))

    def test_schema_one_migration_preserves_existing_cumulative_baselines(self):
        path = pathlib.Path(self.directory) / 'legacy.sqlite3'
        db = sqlite3.connect(path)
        db.executescript('''
            CREATE TABLE attribution(id INTEGER PRIMARY KEY,key TEXT UNIQUE,source TEXT,protocol TEXT,host TEXT,
              destination TEXT,port INTEGER,inbound TEXT,outbound TEXT,kind TEXT,chain TEXT,rule TEXT,payload TEXT);
            CREATE TABLE samples(id INTEGER PRIMARY KEY,token TEXT UNIQUE,core TEXT,time_ms INTEGER,start_ms INTEGER,
              mono_ns INTEGER,upload_total INTEGER,download_total INTEGER,upload INTEGER,download INTEGER,flags TEXT);
            CREATE TABLE usage(sample_id INTEGER,attribution_id INTEGER,start_ms INTEGER,quality TEXT,upload INTEGER,
              download INTEGER,PRIMARY KEY(sample_id,attribution_id,start_ms,quality));
            CREATE TABLE checkpoints(key TEXT PRIMARY KEY,upload INTEGER,download INTEGER,seen_ms INTEGER,mono_ns INTEGER);
            PRAGMA user_version=1;
        ''')
        item = connection(up=100)
        key = json.dumps(('core-a', item['id'], item['start']), separators=(',', ':'))
        db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (1, 'old:1', 'core-a', BASE, BASE, MONO, 100, 0, None, None, '["baseline"]'))
        db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?)', (key, 100, 0, BASE, MONO))
        db.commit()
        db.close()
        ledger = usage.Ledger(path)
        try:
            ledger.ingest({'connections': [connection(up=120)], 'uploadTotal': 120, 'downloadTotal': 0},
                          'core-a', BASE + 1000, MONO + 1000000000)
            ledger.flush()
            self.assertEqual(ledger.db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(ledger.db.execute('SELECT sum(upload) FROM usage').fetchone()[0], 20)
        finally:
            ledger.close()


class CollectorTests(unittest.TestCase):
    def test_required_history_rejects_a_stock_core_instead_of_silently_sampling(self):
        response = unittest.mock.Mock(status=200)
        response.read1.side_effect = [b'{"uploadTotal":0,"downloadTotal":0,"connections":null}', b'']
        with patch.object(usage_collect.http.client, 'HTTPConnection') as factory:
            factory.return_value.getresponse.return_value = response
            controller = usage_collect.Controller({'mihomo': {'external-controller': '127.0.0.1:9090'},
                                                   'usage': {'require_history': True}})
            with self.assertRaises(usage_collect.ControllerError) as error:
                controller.snapshot()
            self.assertEqual(error.exception.code, 'history_unavailable')
            self.assertIsNone(controller.connection)

    def test_shutdown_drain_uses_authenticated_controller_and_requires_success(self):
        with patch.object(usage_collect.http.client, 'HTTPConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 204
            controller = usage_collect.Controller({'mihomo': {'external-controller': '127.0.0.1:9090',
                                                             'secret': 'PRIVATE'}})
            controller.close_connections()
            self.assertEqual(factory.return_value.request.call_args.args, ('DELETE', '/connections'))
            self.assertEqual(factory.return_value.request.call_args.kwargs['headers']['Authorization'], 'Bearer PRIVATE')
            response.status = 401
            with self.assertRaises(usage_collect.ControllerError) as error:
                controller.close_connections()
            self.assertEqual(error.exception.code, 'HTTP_401')
            controller.close()

    def test_http_connection_is_reused_and_auth_is_not_in_url(self):
        response = unittest.mock.Mock(status=200)
        response.read1.side_effect = [b'{"uploadTotal":0,"downloadTotal":0,"connections":null}', b'',
                                     b'{"uploadTotal":1,"downloadTotal":2,"connections":[]}', b'']
        with patch.object(usage_collect.http.client, 'HTTPConnection') as factory:
            factory.return_value.getresponse.return_value = response
            controller = usage_collect.Controller({'mihomo': {'external-controller': '127.0.0.1:9090', 'secret': 'PRIVATE'}})
            controller.snapshot()
            controller.snapshot()
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(factory.return_value.request.call_args.args, ('GET', '/connections?history=true&after=0&limit=4096'))
            self.assertEqual(factory.return_value.request.call_args.kwargs['headers']['Authorization'], 'Bearer PRIVATE')

    def test_http_error_closes_connection_without_emitting_body(self):
        with patch.object(usage_collect.http.client, 'HTTPConnection') as factory:
            factory.return_value.getresponse.return_value.status = 401
            controller = usage_collect.Controller({'mihomo': {'external-controller': '127.0.0.1:9090'}})
            with self.assertRaises(OSError) as error:
                controller.snapshot()
            self.assertEqual(str(error.exception), 'Controller HTTP 401')
            self.assertIsNone(controller.connection)
            factory.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
