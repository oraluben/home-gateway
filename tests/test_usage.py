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


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = pathlib.Path(self.directory) / 'usage.sqlite3'
        self.ledger = usage.Ledger(self.path)
        self.addCleanup(lambda: self.ledger.close())

    def sample(self, second, items=(), up=0, down=0, core='core-a', wall=None):
        return self.ledger.ingest({'connections': list(items), 'uploadTotal': up, 'downloadTotal': down},
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
        self.ledger.prune(BASE + 3660000)
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


class CollectorTests(unittest.TestCase):
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
            self.assertEqual(factory.return_value.request.call_args.args, ('GET', '/connections'))
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
