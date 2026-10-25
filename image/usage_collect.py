"""Independent, bounded-overhead collector; never changes proxy/VPN/network state."""
import fcntl
import http.client
import json
import os
import pathlib
import shutil
import signal
import time

from firewall import controller_listener
from runtime import DATA, load_config, save_json
from usage import Ledger


class ControllerError(OSError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def error_name(error):
    code = getattr(error, 'sqlite_errorname', None) or getattr(error, 'code', None)
    return type(error).__name__ + (':' + code if code else '')


def core_identity():
    status = json.loads((DATA / 'status.json').read_text())
    pid = status.get('components', {}).get('clash', {}).get('pid')
    if type(pid) is not int or pid <= 0:
        raise OSError('Mihomo is not running')
    if pathlib.Path(f'/proc/{pid}/exe').resolve().name != 'mihomo':
        raise OSError('Mihomo process identity is stale')
    # /proc stat comm can contain spaces and parentheses; fields after the final ')' start at field 3.
    stat = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    if stat[0] == 'Z':
        raise OSError('Mihomo has exited')
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return f'{boot}:{pid}:{stat[19]}'


class Controller:
    def __init__(self, config):
        self.host, self.port = controller_listener(config)
        self.secret = config['mihomo'].get('secret', '')
        self.require_history = config.get('usage', {}).get('require_history', False)
        self.connection = None

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def close_connections(self):
        self.connection = http.client.HTTPConnection(self.host, self.port, timeout=1.5)
        headers = {'Authorization': 'Bearer ' + self.secret} if self.secret else {}
        self.connection.request('DELETE', '/connections', headers=headers)
        response = self.connection.getresponse()
        response.read(4096)
        if response.status != 204:
            raise ControllerError(f'HTTP_{response.status}', 'Connection drain failed')

    def snapshot(self, after=0):
        if self.connection is None:
            self.connection = http.client.HTTPConnection(self.host, self.port, timeout=1.5)
        try:
            headers = {'Authorization': 'Bearer ' + self.secret} if self.secret else {}
            self.connection.request('GET', f'/connections?history=true&after={after}&limit=4096', headers=headers)
            response = self.connection.getresponse()
            if response.status != 200:
                raise ControllerError(f'HTTP_{response.status}', f'Controller HTTP {response.status}')
            deadline = time.monotonic() + 1.5
            chunks, length = [], 0
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > 8 * 1024 * 1024 or time.monotonic() > deadline:
                    raise ControllerError('response_limit', 'Controller response limit exceeded')
            result = json.loads(b''.join(chunks))
            if not isinstance(result, dict):
                raise ValueError('Invalid controller response')
            if self.require_history and not (result.get('history') or {}).get('enabled'):
                raise ControllerError('history_unavailable', 'Connection history is required by the selected build')
            return result
        except Exception:
            self.close()
            raise


def main():
    os.umask(0o077)
    # Use the shared data volume so another container cannot write the same ledger.
    # The empty file can persist; the kernel releases the lock on any process exit.
    lock = (DATA / 'usage.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = load_config()
    options = config.get('usage', {})
    retention = options.get('retention_hours', 48)
    budget = options.get('max_mib', 1024)
    if type(retention) is not int or not 1 <= retention <= 8760 or type(budget) is not int or not 16 <= budget <= 16384:
        raise ValueError('Invalid usage retention or storage budget')
    if shutil.disk_usage(DATA).free < 64 * 1024 * 1024:
        raise OSError('Insufficient space for usage accounting')
    ledger = Ledger(DATA / 'usage.sqlite3', max_bytes=budget * 1024 * 1024)
    ledger.retention_hours = retention
    controller = Controller(config)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state = {'state': 'starting', 'pid': os.getpid(), 'last_success_ms': None,
             'last_commit_ms': ledger.last_commit_ms, 'errors': 0, 'sample_ms': None,
             'retention_hours': retention, 'max_mib': budget, 'flags': []}
    state.update(mode='starting', history_sequence=ledger.history_cursor, history_gaps=0)

    def publish():
        state['updated_ms'] = int(time.time() * 1000)
        state['last_commit_ms'] = ledger.last_commit_ms
        state['committed_history_instance'] = ledger.committed_history_instance
        save_json(DATA / 'usage-status.json', state)

    def sample_once():
        began = time.monotonic()
        before = core_identity()
        after = ledger.history_cursor if (ledger.previous or {}).get('core') == before else 0
        snapshot = controller.snapshot(after)
        if before != core_identity():
            raise OSError('Mihomo changed during sampling')
        wall, mono = int(time.time() * 1000), time.monotonic_ns()
        flags = ledger.ingest(snapshot, before, wall, mono)
        state.update(state='running', last_success_ms=wall, flags=flags,
                     sample_ms=round((time.monotonic() - began) * 1000, 2), error=None)
        history_enabled = (snapshot.get('history') or {}).get('enabled') is True
        state.update(mode='close-history' if history_enabled else 'sampling', history_sequence=ledger.history_cursor,
                     history_instance=(snapshot.get('history') or {}).get('instance'),
                     history_gaps=state['history_gaps'] + int('history_lost' in flags or 'history_changed' in flags))

    tick = commit = maintenance = time.monotonic()
    failures, last_publish = 0, 0
    try:
        publish()
        while not stopping:
            try:
                sample_once()
                failures = 0
            except (OSError, ValueError, http.client.HTTPException) as error:
                # Error messages/HTTP bodies may contain credentials: only publish the class.
                state.update(state='degraded', error=error_name(error), errors=state['errors'] + 1)
                failures = min(failures + 1, 5)
                controller.close()
            now = time.monotonic()
            if now - commit >= 5:
                ledger.flush()
                commit = now
            if now - maintenance >= 60:
                ledger.flush()
                ledger.prune(int(time.time() * 1000), retention)
                maintenance = now
                if shutil.disk_usage(DATA).free < 64 * 1024 * 1024:
                    raise OSError('Insufficient space for usage accounting')
            if now - last_publish >= 5:
                publish()
                last_publish = now
            interval = min(30, 2 ** failures) if failures else 1
            tick += interval
            if tick < now:
                tick = now + interval  # Never issue bursts to catch up missed ticks.
            while not stopping and time.monotonic() < tick:
                time.sleep(min(0.2, max(0, tick - time.monotonic())))
        try:
            sample_once()  # Include summaries drained by the appliance supervisor before shutdown.
        except (OSError, ValueError, http.client.HTTPException) as error:
            state.update(error=error_name(error), errors=state['errors'] + 1)
        ledger.flush()
        state['state'] = 'stopped'
        publish()
    except Exception as error:
        state.update(state='error', error=error_name(error))
        try:
            publish()
        except OSError:
            pass
        raise
    finally:
        controller.close()
        ledger.close()
        lock.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never emit a traceback with potentially private configuration/response data.
        print('Usage collector stopped: ' + error_name(error), flush=True)
        raise SystemExit(1)
