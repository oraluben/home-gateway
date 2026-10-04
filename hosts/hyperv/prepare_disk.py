import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tools'))
from common import ROOT, deployment
from cloud_init import write_seed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    config = deployment(args.config)
    if config['host']['backend'] != 'hyperv':
        raise ValueError('VM disk preparation requires the hyperv backend')
    versions = json.loads((ROOT / 'versions.json').read_text())
    output = pathlib.Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    disk = output / 'gateway.vhdx'
    if disk.exists():
        raise ValueError('Refusing to overwrite an existing VM disk')
    write_seed(config, config['host']['hyperv']['MacAddress'], output / 'seed')
    image = ROOT / 'artifacts/ubuntu.img'
    if not image.exists():
        image.parent.mkdir(exist_ok=True)
        urllib.request.urlretrieve(versions['ubuntu']['url'], image)
    if hashlib.sha256(image.read_bytes()).hexdigest() != versions['ubuntu']['sha256']:
        raise ValueError('Ubuntu cloud image checksum mismatch')
    subprocess.run(['qemu-img', 'convert', '-O', 'vhdx', '-o', 'subformat=dynamic', str(image), str(disk)], check=True)
    print('Pinned Ubuntu disk and private cloud-init seed prepared')


if __name__ == '__main__':
    main()
