#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-o Acquire::Retries=2 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)
CURL_OPTS=()
if [[ -n "${BUILD_PROXY:-}" ]]; then
  CURL_OPTS=(--proxy "$BUILD_PROXY")
  APT_OPTS+=(-o "Acquire::https::Proxy::download.docker.com=$BUILD_PROXY")
fi
apt-get "${APT_OPTS[@]}" update
apt-get "${APT_OPTS[@]}" install -y --no-install-recommends ca-certificates curl python3-yaml
install -m 0755 -d /etc/apt/keyrings
curl "${CURL_OPTS[@]}" --fail --silent --show-error --location --retry 2 --retry-max-time 45 --connect-timeout 10 --max-time 60 \
  https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat >/etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
install -d -m 0755 /etc/docker
cat >/etc/docker/daemon.json <<'EOF'
{
  "bridge": "none",
  "iptables": false,
  "ip6tables": false,
  "ip-forward": false,
  "log-driver": "local",
  "log-opts": {"max-size": "10m", "max-file": "3"}
}
EOF
apt-get "${APT_OPTS[@]}" update
apt-get "${APT_OPTS[@]}" install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
cat >/etc/sysctl.d/90-home-gateway.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.default.send_redirects=0
net.ipv4.conf.eth0.send_redirects=0
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
net.ipv4.conf.eth0.rp_filter=0
EOF
sysctl --system >/dev/null
systemctl enable --now docker
docker version --format '{{.Server.Version}}'
docker compose version
