import argparse
import hashlib
import json
import pathlib
import subprocess
import urllib.request
import yaml
from common import ROOT, deployment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    config = deployment(args.config)
    versions = json.loads((ROOT / 'versions.json').read_text())
    output = pathlib.Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    disk = output / 'gateway.vhdx'
    if disk.exists():
        raise ValueError('Refusing to overwrite an existing VM disk')
    image = ROOT / 'artifacts/ubuntu.img'
    if not image.exists():
        image.parent.mkdir(exist_ok=True)
        urllib.request.urlretrieve(versions['ubuntu']['url'], image)
    if hashlib.sha256(image.read_bytes()).hexdigest() != versions['ubuntu']['sha256']:
        raise ValueError('Ubuntu cloud image checksum mismatch')
    subprocess.run(['qemu-img', 'convert', '-O', 'vhdx', '-o', 'subformat=dynamic', str(image), str(disk)], check=True)
    seed = output / 'seed'
    seed.mkdir(exist_ok=True)
    public_key = config['connection']['PublicKey']
    user = config['connection']['User']
    settings = config['hyperv']
    network = config['network']
    mac = ':'.join(settings['MacAddress'][i:i+2] for i in range(0,12,2)).lower()
    user_data = {'hostname': 'home-gateway', 'manage_etc_hosts': True, 'timezone': config.get('timezone','UTC'),
                 'users': [{'name': user, 'groups': ['adm','sudo'], 'shell': '/bin/bash',
                            'sudo': 'ALL=(ALL) NOPASSWD:ALL', 'lock_passwd': True, 'ssh_authorized_keys': [public_key]}],
                 'ssh_pwauth': False, 'disable_root': True, 'package_update': False, 'package_upgrade': False,
                 'runcmd': [['systemctl','enable','--now','ssh']]}
    network_data = {'version': 2, 'ethernets': {'gateway-lan': {'match': {'macaddress':mac},'set-name':'eth0',
                    'dhcp4':False,'dhcp6':False,'accept-ra':False,'addresses':[network['address']],
                    'routes':[{'to':'default','via':network['gateway']}], 'nameservers':{'addresses':network['dns']}}}}
    (seed/'user-data').write_text('#cloud-config\n'+yaml.safe_dump(user_data,sort_keys=False))
    (seed/'network-config').write_text(yaml.safe_dump(network_data,sort_keys=False))
    (seed/'meta-data').write_text('instance-id: home-gateway-'+settings['MacAddress']+'\nlocal-hostname: home-gateway\n')
    print('Pinned Ubuntu disk and private cloud-init seed prepared')


if __name__ == '__main__':
    main()
