"""Test SNMP Trap listener — dengar di UDP 1620."""
import socket
from datetime import datetime

LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 162

print(f"=== SNMP Trap Listener ===")
print(f"Listening di {LISTEN_IP}:{LISTEN_PORT}")
print(f"Tunggu trap... (Ctrl+C untuk stop)")
print()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((LISTEN_IP, LISTEN_PORT))

try:
    while True:
        data, addr = sock.recvfrom(4096)
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] TRAP dari {addr[0]}:{addr[1]}")
        print(f"         Panjang: {len(data)} bytes")
        print(f"         Raw (hex): {data[:80].hex()}")
        print()
except KeyboardInterrupt:
    print("\nSelesai")
finally:
    sock.close()
