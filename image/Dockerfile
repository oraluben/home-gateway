ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=20 update \
 && apt-get -y --no-install-recommends install openconnect vpnc-scripts nftables iproute2 python3 python3-yaml systemd-resolved tini ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY mihomo /usr/local/bin/mihomo
COPY licenses/ /usr/local/share/home-gateway/licenses/
COPY gateway.py network.py configuration.py runtime.py vpn_auth.py vpn-hook.py /app/
RUN chmod 0755 /usr/local/bin/mihomo /app/vpn-hook.py
WORKDIR /app
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/gateway.py"]
