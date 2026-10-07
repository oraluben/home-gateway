import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tailscale_host', ROOT / 'hosts/linux/tailscale.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class TailscaleHostTests(unittest.TestCase):
    def test_existing_unmanaged_installation_is_not_reconfigured(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host, 'RECEIPT', pathlib.Path(directory) / 'receipt'), \
                patch.object(host.os, 'geteuid', return_value=0), \
                patch.object(host.pathlib.Path, 'read_text', return_value='ID=ubuntu\nVERSION_ID="24.04"\n'), \
                patch.object(host.shutil, 'which', return_value='/usr/bin/tailscale'), \
                patch.object(host, 'run', return_value='x86_64') as run:
            with self.assertRaisesRegex(ValueError, 'not managed'):
                host.install('1.102.5')
        run.assert_called_once_with(['uname', '-m'])

    def test_existing_owned_install_preserves_login_routes_and_dns_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            receipt, dropin, sysctl = (root / name for name in ('receipt', 'service', 'sysctl'))
            receipt.write_text('{"schema":1,"firewall":"nftables","version":"1.102.5"}')
            dropin.write_text(host.SERVICE)
            sysctl.write_text(host.SYSCTL_BODY)
            read = pathlib.Path.read_text

            def contents(path, *args, **kwargs):
                return 'ID=ubuntu\nVERSION_ID="24.04"\n' if str(path) == '/etc/os-release' else read(path, *args, **kwargs)

            def execute(args, **kwargs):
                return 'x86_64' if args == ['uname', '-m'] else '1.102.5'

            with patch.object(host, 'RECEIPT', receipt), patch.object(host, 'DROPIN', dropin), \
                    patch.object(host, 'SYSCTL', sysctl), patch.object(host.os, 'geteuid', return_value=0), \
                    patch.object(host.pathlib.Path, 'read_text', contents), \
                    patch.object(host.shutil, 'which', return_value='/usr/bin/tailscale'), \
                    patch.object(host, 'run', side_effect=execute) as run:
                self.assertFalse(host.install('1.102.5')['changed'])
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ['uname', '-m'], ['tailscale', 'version'], ['tailscale', 'set', '--auto-update=false']])

    def test_compatibility_refuses_to_overwrite_unrelated_sysctl_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / 'sysctl'
            path.write_text('unrelated configuration\n')
            with patch.object(host, 'SYSCTL', path), patch.object(host, 'DROPIN', root / 'service'), \
                    patch.object(host, 'run') as run, self.assertRaisesRegex(ValueError, 'differs'):
                host.install_compatibility()
            run.assert_not_called()
            self.assertEqual(path.read_text(), 'unrelated configuration\n')
            self.assertFalse((root / 'service').exists())


if __name__ == '__main__':
    unittest.main()
