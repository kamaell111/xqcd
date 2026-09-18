#!/bin/bash
# Script ganti IP OLT: LAN (kantor) atau Publik (rumah)

if [ "$1" == "home" ]; then
    sed -i 's/^SEED_OLT_IP=.*/SEED_OLT_IP=103.46.8.46/' .env
    sed -i 's/^SEED_OLT_PORT=.*/SEED_OLT_PORT=779/' .env
    echo "✅ Mode RUMAH (IP publik)"
elif [ "$1" == "office" ]; then
    sed -i 's/^SEED_OLT_IP=.*/SEED_OLT_IP=136.1.1.200/' .env
    sed -i 's/^SEED_OLT_PORT=.*/SEED_OLT_PORT=23/' .env
    echo "✅ Mode KANTOR (IP LAN)"
else
    echo "Usage: ./switch-network.sh {home|office}"
    echo "  home   = pakai IP publik (dari rumah)"
    echo "  office = pakai IP LAN (dari kantor)"
    exit 1
fi

echo ""
echo "Current:"
grep SEED_OLT .env
