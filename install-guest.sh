#!/bin/bash
set -euo pipefail
cd /opt/home-gateway
chmod 0700 data
chmod 0700 config config/secrets
cat >/etc/systemd/system/home-gateway-base.service <<'EOF'
[Unit]
Description=Home gateway base forwarding and DNS
After=network-online.target systemd-resolved.service
Wants=network-online.target
Before=home-gateway.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 /opt/home-gateway/apply-base.py
[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/home-gateway.service <<'EOF'
[Unit]
Description=Home gateway proxy and VPN appliance
After=docker.service home-gateway-base.service
Requires=docker.service home-gateway-base.service
StartLimitIntervalSec=infinity
StartLimitBurst=3
[Service]
Type=simple
WorkingDirectory=/opt/home-gateway
ExecStart=/usr/bin/docker compose up --no-build --pull never --abort-on-container-exit --exit-code-from gateway
ExecStop=/usr/bin/docker compose down --timeout 25
ExecStopPost=-/usr/sbin/nft delete table inet home_gateway_proxy
Restart=on-failure
RestartSec=5
TimeoutStopSec=40
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
cat >/etc/systemd/system/home-gateway-dns.service <<'EOF'
[Unit]
Description=Apply pushed VPN DNS policy to the gateway resolver
After=home-gateway-base.service
Requires=home-gateway-base.service
StartLimitIntervalSec=infinity
StartLimitBurst=3
[Service]
ExecStart=/usr/bin/python3 /opt/home-gateway/dns-policy.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now home-gateway-base
systemctl enable --now home-gateway-dns
# Enable at boot, but start only after the image has been built and validated.
systemctl enable home-gateway
