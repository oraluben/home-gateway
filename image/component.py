"""Finite process retries; a stable session can replenish the retry budget."""
import os
import signal
import subprocess
import time
from network import DATA, read_policy, save_json


class Component:
    def __init__(self, name, command, max_attempts, password=None, *,
                 retry_delay=5, retry_max_delay=5, stable_reset_seconds=0):
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 20:
            raise ValueError('Component attempts must be between 1 and 20')
        if not 0 < retry_delay <= retry_max_delay or stable_reset_seconds < 0:
            raise ValueError('Invalid component retry timing')
        self.name, self.command = name, command
        self.max_attempts, self.password = max_attempts, password
        self.retry_delay, self.retry_max_delay = retry_delay, retry_max_delay
        self.stable_reset_seconds = stable_reset_seconds
        self.process, self.log = None, None
        self.attempts, self.next_attempt, self.last_exit = 0, 0, None
        self.healthy_since = None

    def poll(self, healthy=False):
        now = time.monotonic()
        if self.process and self.process.poll() is not None:
            self.last_exit = self.process.returncode
            self.process = None
            self.log.write(f'\n[{time.strftime("%Y-%m-%dT%H:%M:%S%z")}] {self.name}: exited with {self.last_exit}\n'.encode())
            self.log.close()
            self.healthy_since = None
            delay = min(self.retry_delay * 2 ** (self.attempts - 1), self.retry_max_delay)
            self.next_attempt = now + delay
            print(f'{self.name}: exited with {self.last_exit}; attempt {self.attempts}/{self.max_attempts}', flush=True)
            if self.name == 'vpn':
                self._vpn_down()
        if self.process:
            if not healthy:
                self.healthy_since = None
            elif self.healthy_since is None:
                self.healthy_since = now
            elif self.stable_reset_seconds and now - self.healthy_since >= self.stable_reset_seconds and self.attempts > 1:
                self.attempts = 1
                print(f'{self.name}: retry budget restored after a stable session', flush=True)
        if not self.process and self.attempts < self.max_attempts and now >= self.next_attempt:
            logs = DATA / 'logs'
            logs.mkdir(exist_ok=True)
            self.log = (logs / (self.name + '.log')).open('ab', buffering=0)
            self.attempts += 1
            self.log.write(f'\n[{time.strftime("%Y-%m-%dT%H:%M:%S%z")}] {self.name}: starting attempt {self.attempts}/{self.max_attempts}\n'.encode())
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE if self.password else subprocess.DEVNULL,
                                            stdout=self.log, stderr=self.log, start_new_session=True)
            if self.password:
                self.process.stdin.write((self.password.read_text().rstrip('\r\n') + '\n').encode())
                self.process.stdin.close()
            print(f'{self.name}: started, attempt {self.attempts}/{self.max_attempts}', flush=True)

    def status(self):
        state = 'running' if self.process else ('stopped' if self.attempts >= self.max_attempts else 'retry-wait')
        return {'state': state, 'attempts': self.attempts, 'max_attempts': self.max_attempts,
                'last_exit': self.last_exit,
                'retry_in_seconds': max(0, round(self.next_attempt - time.monotonic(), 1)) if state == 'retry-wait' else None}

    def _vpn_down(self):
        policy = read_policy()
        policy['up'] = False
        save_json(DATA / 'vpn-policy.json', policy)

    def stop(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        if self.log and not self.log.closed:
            self.log.close()
        self.process = None
        self.healthy_since = None
        if self.name == 'vpn':
            self._vpn_down()

    def disable(self):
        self.stop()
        self.attempts = self.max_attempts

    def retry(self):
        self.stop()
        self.attempts, self.next_attempt, self.last_exit = 0, 0, None
