import os
import pathlib
import sys
import yaml
sys.path.insert(0, str(pathlib.Path(__file__).parent / 'image'))
os.environ['HOME_GATEWAY_DATA'] = str(pathlib.Path(__file__).parent / 'data')
os.environ.setdefault('HOME_GATEWAY_CONFIG', str(pathlib.Path(__file__).parent / 'config/gateway.yaml'))
from network import DATA, apply_base, proxy_off, read_policy, run
from runtime import load_config

config = load_config()
proxy_off()
apply_base(config, read_policy())
interface = config['network']['interface']
addresses = __import__('json').loads(run(['ip', '-j', '-4', 'address', 'show', 'dev', interface]).stdout)
address = next(a['local'] for entry in addresses for a in entry['addr_info'] if a['scope'] == 'global')
resolved = pathlib.Path('/etc/systemd/resolved.conf.d/90-home-gateway.conf')
resolved.parent.mkdir(exist_ok=True)
resolved.write_text('[Resolve]\nDNSStubListenerExtra=' + address + '\nLLMNR=no\nMulticastDNS=no\n')
run(['systemctl', 'restart', 'systemd-resolved'])
