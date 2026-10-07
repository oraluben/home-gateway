#!/bin/bash
set -euo pipefail
cd /opt/home-gateway
chmod 0700 data
chmod 0700 config config/secrets
install -m 0755 gatewayctl.sh /usr/local/bin/gatewayctl
# The appliance owns its policy rules. networkd otherwise removes them when
# package updates restart it, while the proxy and VPN continue running.
install -d -m 0755 /etc/systemd/networkd.conf.d
cat >/etc/systemd/networkd.conf.d/90-home-gateway.conf <<'EOF'
[Network]
ManageForeignRoutingPolicyRules=no
EOF
chmod 0644 /etc/systemd/networkd.conf.d/90-home-gateway.conf
# No network restart during installation; the routing supervisor also repairs
# missing rules until networkd next reads this setting at startup.
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
cat >/etc/systemd/system/home-gateway-tailscale-policy.service <<'EOF'
[Unit]
Description=Publish pushed VPN destinations for the Tailscale exit node
After=tailscaled.service home-gateway-base.service
Wants=tailscaled.service
StartLimitIntervalSec=infinity
StartLimitBurst=3
[Service]
ExecStart=/usr/bin/python3 /opt/home-gateway/tailscale-policy.py
Restart=on-failure
RestartSec=15
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
if python3 -c 'import yaml; c=yaml.safe_load(open("config/gateway.yaml")); raise SystemExit(0 if c["network"].get("tailscale_exit",False) else 1)'; then
    # A first install has a unit file but no loaded unit state to reset yet.
    systemctl reset-failed home-gateway-tailscale-policy 2>/dev/null || true
    systemctl enable home-gateway-tailscale-policy
    systemctl restart home-gateway-tailscale-policy
else
    systemctl disable --now home-gateway-tailscale-policy
fi
# Enable at boot, but start only after the image has been built and validated.
systemctl enable home-gateway
