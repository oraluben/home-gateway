"""Shared Ubuntu appliance seed; disk and hypervisor creation belong to adapters."""
import ipaddress
import pathlib
import re
import yaml


def write_seed(config, mac_address, destination):
    if not re.fullmatch(r'[0-9a-fA-F]{12}', mac_address):
        raise ValueError('Expected a twelve-digit MAC address')
    interface = config['runtime']['network']['interface']
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', interface):
        raise ValueError('Invalid network interface name')
    network = config['network']
    ipaddress.IPv4Interface(network['address'])
    ipaddress.IPv4Address(network['gateway'])
    connection = config['connection']
    mac = ':'.join(mac_address[i:i + 2] for i in range(0, 12, 2)).lower()
    user_data = {
        'hostname': 'home-gateway', 'manage_etc_hosts': True, 'timezone': config.get('timezone', 'UTC'),
        'users': [{'name': connection['User'], 'groups': ['adm', 'sudo'], 'shell': '/bin/bash',
                   'sudo': 'ALL=(ALL) NOPASSWD:ALL', 'lock_passwd': True,
                   'ssh_authorized_keys': [connection['PublicKey']]}],
        'ssh_pwauth': False, 'disable_root': True, 'package_update': False, 'package_upgrade': False,
        'runcmd': [['systemctl', 'enable', '--now', 'ssh']]}
    network_data = {'version': 2, 'ethernets': {'gateway-lan': {
        'match': {'macaddress': mac}, 'set-name': interface,
        'dhcp4': False, 'dhcp6': False, 'accept-ra': False, 'addresses': [network['address']],
        'routes': [{'to': 'default', 'via': network['gateway']}],
        'nameservers': {'addresses': network['dns']}}}}
    destination = pathlib.Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'user-data').write_text('#cloud-config\n' + yaml.safe_dump(user_data, sort_keys=False))
    (destination / 'network-config').write_text(yaml.safe_dump(network_data, sort_keys=False))
    (destination / 'meta-data').write_text('instance-id: home-gateway-' + mac_address.lower() + '\nlocal-hostname: home-gateway\n')
