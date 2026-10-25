"""Offline integration test: localhost traffic only; no live gateway configuration."""
import argparse
import http.client
import json
import pathlib
import secrets
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'image'))
from usage import Ledger


def free_port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


class Origin(socketserver.BaseRequestHandler):
    def handle(self):
        request = b''
        while b'\r\n\r\n' not in request:
            part = self.request.recv(4096)
            if not part:
                return
            request += part
        body = b'x' * 32768
        response = b'HTTP/1.1 200 OK\r\nContent-Length: 32768\r\nConnection: close\r\n\r\n' + body
        with self.server.lock:
            self.server.upload += len(request)
            self.server.download += len(response)
        self.request.sendall(response)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request[1].sendto(self.request[0], self.client_address)


def exercise(binary, capacity=4096):
    with tempfile.TemporaryDirectory(prefix='mihomo-history-check-') as directory:
        folder = pathlib.Path(directory)
        controller, mixed = free_port(), free_port()
        secret = secrets.token_hex(16)
        config = {'mixed-port': mixed, 'external-controller': f'127.0.0.1:{controller}', 'secret': secret,
                  'mode': 'rule', 'rules': ['MATCH,DIRECT'], 'log-level': 'warning',
                  'connection-history': {'capacity': capacity, 'log': False}}
        (folder / 'config.json').write_text(json.dumps(config))
        ledger = Ledger(folder / 'usage.sqlite3')
        origin = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Origin)
        origin.lock, origin.upload, origin.download = threading.Lock(), 0, 0
        echo = socketserver.ThreadingUDPServer(('127.0.0.1', 0), Echo)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (origin, echo)]
        for thread in threads:
            thread.start()
        log = (folder / 'core.log').open('wb')
        process = subprocess.Popen([binary, '-d', str(folder / 'runtime'), '-f', str(folder / 'config.json')],
                                   stdout=log, stderr=log)

        def api(path, method='GET'):
            stream = http.client.HTTPConnection('127.0.0.1', controller, timeout=3)
            try:
                stream.request(method, path, headers={'Authorization': 'Bearer ' + secret})
                response = stream.getresponse()
                raw = response.read()
                if response.status not in (200, 204):
                    raise RuntimeError('Test controller returned HTTP ' + str(response.status))
                return json.loads(raw) if raw else None
            finally:
                stream.close()

        def snapshot(after):
            return api(f'/connections?history=true&after={after}&limit=4096')

        def ingest(value):
            return ledger.ingest(value, 'test-core', int(time.time() * 1000), time.monotonic_ns())

        def settled(after):
            deadline = time.monotonic() + 5
            last = None
            while time.monotonic() < deadline:
                value = snapshot(after)
                marker = (value['uploadTotal'], value['downloadTotal'], value['history']['latestSequence'])
                if not value['connections'] and marker == last:
                    return value
                last = marker
                time.sleep(0.05)
            raise RuntimeError('Test connections failed to settle')

        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    initial = snapshot(0)
                    break
                except OSError:
                    if time.monotonic() > deadline or process.poll() is not None:
                        raise RuntimeError('Test core failed to start') from None
                    time.sleep(0.1)
            if 'history' in api('/connections'):
                raise AssertionError('Stock API unexpectedly changed')
            # The controller can listen before mixed-inbound initialization finishes.
            # Warm up through the real proxy, then baseline that traffic away.
            while True:
                client = http.client.HTTPConnection('127.0.0.1', mixed, timeout=3)
                try:
                    client.request('GET', f'http://127.0.0.1:{origin.server_address[1]}/ready')
                    response = client.getresponse()
                    if response.status == 200 and response.read() == b'x' * 32768:
                        break
                except OSError:
                    pass
                finally:
                    client.close()
                if time.monotonic() > deadline:
                    raise RuntimeError('Test mixed inbound failed to become ready')
                time.sleep(0.1)
            initial = settled(0)
            origin_up, origin_down = origin.upload, origin.download
            ingest(initial)
            ledger.flush()
            for _ in range(40):
                client = http.client.HTTPConnection('127.0.0.1', mixed, timeout=3)
                client.request('GET', f'http://127.0.0.1:{origin.server_address[1]}/fixture')
                response = client.getresponse()
                body = response.read()
                if body != b'x' * 32768:
                    raise AssertionError(f'Wrong localhost response: HTTP {response.status}, bytes {len(body)}, prefix {body[:160]!r}')
                client.close()
            value = settled(ledger.history_cursor)
            if capacity == 4096:
                assert value['downloadTotal'] - initial['downloadTotal'] == origin.download - origin_down
                assert value['uploadTotal'] - initial['uploadTotal'] == origin.upload - origin_up
                assert len({r['id'] for r in value['history']['records']}) == 40
                other = snapshot(initial['history']['nextSequence'])
                assert other['history']['records'] == value['history']['records'], 'Reading consumed history'
            flags = ingest(value)
            ledger.flush()
            if capacity < 40:
                assert 'history_lost' in flags
                return {'capacity': capacity, 'overflow_detected': True}
            # Replaying the same history, then restarting the collector, must not add bytes.
            ingest(value)
            ledger.flush()
            ledger.close()
            ledger = Ledger(folder / 'usage.sqlite3')
            assert ledger.history_cursor == value['history']['nextSequence']

            # SOCKS5 UDP traffic, closed through the controller before the ledger samples it.
            control = socket.create_connection(('127.0.0.1', mixed), timeout=3)
            control.sendall(b'\x05\x01\x00')
            assert control.recv(2) == b'\x05\x00'
            control.sendall(b'\x05\x03\x00\x01' + bytes(6))
            answer = control.recv(64)
            assert answer[:4] == b'\x05\x00\x00\x01'
            udp_target = ('127.0.0.1', struct.unpack('!H', answer[8:10])[0])
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
                datagram.settimeout(3)
                payload = b'y' * 128
                packet = b'\x00\x00\x00\x01' + socket.inet_aton('127.0.0.1') + struct.pack('!H', echo.server_address[1]) + payload
                for _ in range(20):
                    datagram.sendto(packet, udp_target)
                    assert datagram.recvfrom(4096)[0][10:] == payload
                active = snapshot(ledger.history_cursor)
                for item in active['connections'] or []:
                    api('/connections/' + item['id'], 'DELETE')
            control.close()
            value = settled(ledger.history_cursor)
            ingest(value)
            ledger.flush()
            observed = ledger.db.execute('SELECT sum(upload),sum(download) FROM usage').fetchone()
            assert tuple(observed) == (value['uploadTotal']-initial['uploadTotal'], value['downloadTotal']-initial['downloadTotal'])
            assert observed[0] == origin.upload - origin_up + 20 * 128
            assert observed[1] == origin.download - origin_down + 20 * 128
            assert not ledger.db.execute("SELECT flags FROM samples WHERE flags LIKE '%invalid%' OR flags LIKE '%history_lost%'").fetchall()
            return {'tcp_short_connections': 40, 'udp_packets': 20, 'upload': observed[0], 'download': observed[1],
                    'unattributed_bytes': 0, 'replay_and_restart_idempotent': True,
                    'integrity': ledger.db.execute('PRAGMA quick_check').fetchone()[0]}
        except Exception:
            log.flush()
            print((folder / 'core.log').read_text(errors='replace')[-4000:], file=sys.stderr)
            raise
        finally:
            ledger.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.close()
            for server in (origin, echo):
                server.shutdown()
                server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    print(json.dumps({'normal': exercise(args.binary), 'overflow': exercise(args.binary, 8)}))


if __name__ == '__main__':
    main()
