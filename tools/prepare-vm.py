"""Compatibility entry point for the Hyper-V disk adapter."""
import runpy
from common import ROOT

if __name__ == '__main__':
    runpy.run_path(str(ROOT / 'hosts/hyperv/prepare_disk.py'), run_name='__main__')
