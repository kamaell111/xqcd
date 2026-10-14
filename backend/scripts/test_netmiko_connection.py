"""
Script tes manual koneksi Netmiko ke OLT ZTE ZXAN (Task #1 PRD).

CATATAN PENTING:
- Device ini diakses via TELNET di port custom (779), BUKAN SSH port 22.
  Ini beda dari asumsi default di backend/ssh_client.py saat ini.
- device_type yang dicoba: "zte_zxros_telnet" (driver resmi Netmiko untuk
  ZTE ZXROS/ZXAN, cocok dengan gaya prompt "ZXAN>" / "ZXAN#" di transcript).
- Script ini HARUS dijalankan di mesin yang punya akses jaringan ke OLT
  (bukan di sandbox AI ini — AI tidak bisa menjangkau IP device).

Cara pakai:
    cd xqcd
    source venv/bin/activate
    python test_netmiko_connection.py

Kredensial diminta interaktif (tidak di-hardcode) supaya aman.
"""

import getpass
import sys

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException

# Default sesuai transcript telnet yang sudah pernah berhasil
DEFAULT_HOST = "10.0.0.1"  # ganti sesuai OLT Anda
DEFAULT_PORT = 23


def main():
    host = input(f"Host OLT [{DEFAULT_HOST}]: ").strip() or DEFAULT_HOST
    port_raw = input(f"Port telnet [{DEFAULT_PORT}]: ").strip()
    port = int(port_raw) if port_raw else DEFAULT_PORT
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    need_enable = input("Perlu masuk mode enable? (y/n) [y]: ").strip().lower() or "y"
    secret = getpass.getpass("Enable password: ") if need_enable == "y" else ""

    device = {
        "device_type": "zte_zxros_telnet",
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "secret": secret,
        "timeout": 30,
        "session_log": "netmiko_session.log",  # rekam raw session buat debug
    }

    print(f"\n[*] Menyambung ke {host}:{port} (device_type=zte_zxros_telnet)...")
    try:
        conn = ConnectHandler(**device)
    except NetmikoAuthenticationException as e:
        print(f"[GAGAL] Autentikasi ditolak: {e}")
        sys.exit(1)
    except NetmikoTimeoutException as e:
        print(f"[GAGAL] Timeout / tidak bisa konek: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[GAGAL] Error tidak terduga saat connect: {type(e).__name__}: {e}")
        sys.exit(1)

    print("[OK] Berhasil connect & login.")

    try:
        prompt = conn.find_prompt()
        print(f"[*] Prompt terbaca: {prompt}")

        if secret:
            try:
                conn.enable()
                print(f"[OK] Berhasil masuk mode enable. Prompt sekarang: {conn.find_prompt()}")
            except Exception as e:
                print(f"[WARNING] Gagal enable: {type(e).__name__}: {e}")

        print("\n[*] Menjalankan 'show version'...")
        out = conn.send_command("show version", read_timeout=20)
        print("--- OUTPUT show version ---")
        print(out)
        print("--- END ---\n")

        print("[*] Menjalankan 'show running-config' (bisa panjang, read_timeout 60s)...")
        out2 = conn.send_command("show running-config", read_timeout=60)
        print(f"[OK] Panjang output: {len(out2)} karakter (tidak dicetak semua di sini)")

    finally:
        conn.disconnect()
        print("[*] Disconnect. Detail raw session ada di netmiko_session.log")


if __name__ == "__main__":
    main()
