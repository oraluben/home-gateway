#!/bin/bash
set -euo pipefail
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
