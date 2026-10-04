import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

PROJECT = pathlib.Path(__file__).resolve().parent
DATA = PROJECT / 'data'
os.environ['HOME_GATEWAY_DATA'] = str(DATA)
os.environ.setdefault('HOME_GATEWAY_CONFIG', str(PROJECT / 'config/gateway.yaml'))
sys.path.insert(0, str(PROJECT / 'image'))
from configuration import render_mihomo
from runtime import load_config
PHASE = 'starting'


def write(path, content):
    temporary = path.with_name(path.name + '.update.tmp')
    temporary.write_bytes(content)
    temporary.chmod(0o600)
    temporary.replace(path)


def download(source):
    errors = []
    if urllib.parse.urlsplit(source['url']).scheme != 'https':
        raise ValueError('The subscription URL must use HTTPS')
    for proxy in (None, 'http://127.0.0.1:7897'):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {'https': proxy, 'http': proxy} if proxy else {}))
            request = urllib.request.Request(source['url'], headers={
                'User-Agent': source.get('user_agent', 'clash-verge')})
            with opener.open(request, timeout=12) as response:
                if urllib.parse.urlsplit(response.url).scheme != 'https':
                    raise ValueError('The subscription redirected to an unencrypted URL')
                body = response.read(5 * 1024 * 1024 + 1)
            if len(body) > 5 * 1024 * 1024:
                raise ValueError('The subscription exceeds the size limit')
            return body
        except Exception as error:
            errors.append('HTTP ' + str(error.code) if isinstance(error, urllib.error.HTTPError) else type(error).__name__)
    raise RuntimeError('Subscription download failed: ' + ', '.join(errors))


def api(config, path, method='GET', payload=None):
    endpoint = 'http://' + config['mihomo']['external-controller']
    headers = {'Authorization': 'Bearer ' + config['mihomo']['secret'], 'Content-Type': 'application/json'}
    request = urllib.request.Request(endpoint + path, headers=headers, method=method,
                                     data=json.dumps(payload).encode() if payload is not None else None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=8) as response:
        body = response.read()
        return json.loads(body) if body else None


def selections(config):
    return {name: item['now'] for name, item in api(config, '/proxies')['proxies'].items()
            if item.get('type', '').lower() == 'selector' and item.get('now')}


def restore_selections(config, previous):
    proxies = api(config, '/proxies')['proxies']
    patterns = config['subscription'].get('fallback_filters', {})
    selected = {}
    for name, item in proxies.items():
        if item.get('type', '').lower() != 'selector':
            continue
        options = item.get('all', [])
        choice = previous.get(name)
        if choice not in options:
            pattern = patterns.get(name)
            choice = next((option for option in options if re.search(pattern, option, re.I)), None) if pattern else None
            if pattern and choice is None:
                raise ValueError('The updated subscription has no suitable node for a required group')
        if choice:
            api(config, '/proxies/' + urllib.parse.quote(name, safe=''), 'PUT', {'name': choice})
            selected[name] = choice
    return selected


def refresh(config):
    global PHASE
    PHASE = 'read-live-config'
    directory = DATA / 'subscription'
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    cache = directory / 'current.yaml'
    runtime = DATA / 'mihomo/generated.yaml'
    original_cache = cache.read_bytes() if cache.is_file() else None
    original_runtime = runtime.read_bytes()
    mode = api(config, '/configs').get('mode', 'rule')
    PHASE = 'download'
    body = download(config['subscription'])
    PHASE = 'validate'
    subscription = yaml.safe_load(body)
    generated = render_mihomo(config, subscription)
    generated['mode'] = mode
    candidate = DATA / 'mihomo/validate-subscription.yaml'
    write(candidate, yaml.safe_dump(generated, allow_unicode=True, sort_keys=False).encode())
    try:
        validation = subprocess.run(['docker', 'exec', 'home-gateway', 'mihomo', '-t',
                                     '-d', '/data/mihomo', '-f', '/data/mihomo/validate-subscription.yaml'],
                                    capture_output=True, text=True, timeout=20)
        if validation.returncode:
            raise ValueError('Mihomo rejected the downloaded subscription')
    finally:
        candidate.unlink(missing_ok=True)
    previous = selections(config)
    changed = body != original_cache
    if changed:
        PHASE = 'apply'
        try:
            write(cache, body)
            write(runtime, yaml.safe_dump(generated, allow_unicode=True, sort_keys=False).encode())
            api(config, '/configs?force=true', 'PUT', {'path': '/data/mihomo/generated.yaml'})
            selected = restore_selections(config, previous)
            if original_cache:
                write(directory / 'previous.yaml', original_cache)
        except Exception:
            if original_cache is None:
                cache.unlink(missing_ok=True)
            else:
                write(cache, original_cache)
            write(runtime, original_runtime)
            try:
                api(config, '/configs?force=true', 'PUT', {'path': '/data/mihomo/generated.yaml'})
                restore_selections(config, previous)
            except Exception:
                raise RuntimeError('Subscription update failed and the old configuration could not be reloaded') from None
            raise RuntimeError('Subscription update failed; the previous configuration was restored') from None
    else:
        selected = previous
    return {'state': 'success', 'changed': changed, 'sha256': hashlib.sha256(body).hexdigest(),
            'nodes': len(subscription.get('proxies', [])), 'groups': len(subscription['proxy-groups']),
            'rules': len(subscription['rules']), 'selected': selected}


def main():
    config = load_config()
    if not config.get('subscription', {}).get('enabled', False):
        raise RuntimeError('Subscription updates are not enabled')
    directory = DATA / 'subscription'
    directory.mkdir(mode=0o700, exist_ok=True)
    lock = directory / 'update.lock'
    with lock.open('a') as stream:
        lock.chmod(0o600)
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status_path = DATA / 'subscription-status.json'
        previous = json.loads(status_path.read_text()) if status_path.is_file() else {}
        now = datetime.datetime.now().astimezone().isoformat()
        try:
            status = refresh(config)
            status.update(last_attempt=now, last_success=now, error=None)
            write(status_path, (json.dumps(status, ensure_ascii=False, indent=2) + '\n').encode())
            print(json.dumps(status, ensure_ascii=False))
        except Exception as error:
            # Error details and download URLs can contain credentials; only report the error type.
            previous.update(state='failed', last_attempt=now, error=type(error).__name__, phase=PHASE,
                            using_last_good=(directory / 'current.yaml').is_file())
            if isinstance(error, urllib.error.HTTPError):
                previous['http_status'] = error.code
            if PHASE == 'download' and isinstance(error, RuntimeError):
                previous['download_error'] = str(error)
            write(status_path, (json.dumps(previous, ensure_ascii=False, indent=2) + '\n').encode())
            print(json.dumps(previous, ensure_ascii=False))
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
