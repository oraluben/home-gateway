#!/bin/sh
# Thin SSH entry point: diagnostic logic lives in the appliance image.
set -eu
if [ "$(id -u)" -ne 0 ]; then
    exec sudo /usr/local/bin/gatewayctl "$@"
fi
if [ "$#" -eq 0 ]; then
    set -- status
fi
case "$1" in
    logs-system)
        exec journalctl -u home-gateway -u home-gateway-dns --no-pager -n 60
        ;;
    start)
        systemctl reset-failed home-gateway home-gateway-dns
        exec systemctl start home-gateway-dns home-gateway
        ;;
    stop)
        exec systemctl stop home-gateway
        ;;
esac
if [ "$(docker inspect --format '{{.State.Running}}' home-gateway 2>/dev/null || true)" != true ]; then
    echo 'Gateway container is not running. Service state:' >&2
    systemctl is-active home-gateway home-gateway-dns docker systemd-resolved || true
    echo 'Use gatewayctl logs-system, then gatewayctl start after resolving the failure.' >&2
    exit 1
fi
exec docker exec home-gateway gatewayctl "$@"
