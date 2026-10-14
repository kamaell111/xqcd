#!/bin/bash
# Script ganti IP OLT di .env — pakai env var agar tidak ada IP asli di source.
#
# Pemakaian:
#   OFFICE_OLT_IP=10.0.0.1   OFFICE_OLT_PORT=23   ./switch-network.sh office
#   HOME_OLT_IP=1.2.3.4      HOME_OLT_PORT=779    ./switch-network.sh home
#
# Simpan nilai asli di shell profile (~/.bashrc) atau file lokal yang tidak di-commit.

set -e

mode="${1:-}"
if [ "$mode" == "home" ]; then
    ip="${HOME_OLT_IP:?Set HOME_OLT_IP env var dulu}"
    port="${HOME_OLT_PORT:-779}"
    label="RUMAH (publik)"
elif [ "$mode" == "office" ]; then
    ip="${OFFICE_OLT_IP:?Set OFFICE_OLT_IP env var dulu}"
    port="${OFFICE_OLT_PORT:-23}"
    label="KANTOR (LAN)"
else
    echo "Usage: ./switch-network.sh {home|office}"
    echo "Set env var dulu, contoh:"
    echo "  export OFFICE_OLT_IP=10.0.0.1 OFFICE_OLT_PORT=23"
    echo "  export HOME_OLT_IP=1.2.3.4   HOME_OLT_PORT=779"
    exit 1
fi

sed -i "s/^SEED_OLT_IP=.*/SEED_OLT_IP=${ip}/" .env
sed -i "s/^SEED_OLT_PORT=.*/SEED_OLT_PORT=${port}/" .env
echo "✅ Mode ${label}: ${ip}:${port}"
echo ""
grep SEED_OLT .env
