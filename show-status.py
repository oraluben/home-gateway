"""Compatibility entry point; the image owns gateway diagnostics."""
import subprocess

raise SystemExit(subprocess.call(['/usr/local/bin/gatewayctl', 'status', '--json']))
