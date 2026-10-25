#!/bin/bash
set -euo pipefail
if ! python3 - <<'PY'
import sys
sys.path.insert(0, '/opt/home-gateway/image')
import yaml
from topology import component_enabled
with open('/opt/home-gateway/config/gateway.yaml') as stream:
    config = yaml.safe_load(stream)
raise SystemExit(0 if component_enabled(config, 'clash') and component_enabled(config, 'subscription', False) else 1)
PY
then
    systemctl disable --now home-gateway-subscription.timer 2>/dev/null || true
    exit 0
fi
interval=$(python3 - <<'PY'
import pathlib
import yaml
config = yaml.safe_load(pathlib.Path('/opt/home-gateway/config/gateway.yaml').read_text())
interval = int(config['subscription'].get('interval_seconds', 86400))
if not 900 <= interval <= 7 * 86400:
    raise ValueError('The subscription interval must be between 15 minutes and seven days')
print(interval)
PY
)
cat >/etc/systemd/system/home-gateway-subscription.service <<'EOF'
[Unit]
Description=Refresh the gateway's Clash subscription
After=home-gateway.service network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/home-gateway/update-subscription.py
TimeoutStartSec=120
UMask=0077
EOF
cat >/etc/systemd/system/home-gateway-subscription.timer <<EOF
[Unit]
Description=Daily gateway subscription refresh
[Timer]
OnBootSec=3min
OnUnitActiveSec=${interval}s
AccuracySec=1min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now home-gateway-subscription.timer
