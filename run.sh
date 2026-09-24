#!/bin/bash
# =================== ZTE OLT Manager — Launcher ===================
# Cara pakai:
#   1. chmod +x run.sh   (sekali saja)
#   2. ./run.sh
#
# Script ini akan:
#   - Copy .env.example jadi .env (kalau belum ada)
#   - Buat virtual environment (kalau belum ada)
#   - Install dependencies
#   - Start uvicorn di port 8001

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================"
echo "  ZTE OLT Manager — Startup"
echo "======================================"
echo ""

# 1. Cek Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 tidak ditemukan. Install dulu: sudo apt install python3 python3-venv"
    exit 1
fi

# 2. Cek .env
if [ ! -f "backend/.env" ]; then
    echo "[.env] belum ada — copy dari .env.example"
    cp backend/.env.example backend/.env
    echo ""
    echo "PENTING: Edit dulu backend/.env — isi IP OLT, user, password!"
    echo "         nano backend/.env"
    echo ""
    read -p "Sudah edit .env? (y/n) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Edit dulu: nano backend/.env"
        exit 0
    fi
fi

# 3. Buat venv kalau belum ada
if [ ! -d ".venv" ]; then
    echo "[venv] Membuat virtual environment..."
    python3 -m venv .venv
fi

# 4. Aktifkan venv
source .venv/bin/activate

# 5. Install dependencies
echo "[deps] Install dependencies (mungkin lama pertama kali)..."
pip install -q --upgrade pip
pip install -q -r backend/requirements.txt

# 6. Start uvicorn
echo ""
echo "[run] Start aplikasi di http://localhost:8001"
echo "      Login: cek backend/.env (SEED_ADMIN_USERNAME / SEED_ADMIN_PASSWORD)"
echo "      Stop: Ctrl+C"
echo ""
cd backend
uvicorn app:app --host 0.0.0.0 --port 8001
