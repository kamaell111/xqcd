"""SNMP Trap receiver untuk OLT ZTE ZXAN.

Mendengar UDP port 1620, extract eventid dari community string ZTE,
trigger light sync setelah debounce (handle trap storm).

Design principle: trap = hint/pemicu. Bukan source of truth.
"""
import asyncio
import re
import time
from typing import Callable, Optional


# =================== KONFIGURASI ===================
LISTEN_PORT = 1620        # non-privileged. Redirect 162→1620 via iptables
DEBOUNCE_WINDOW = 3.0     # detik tenang sebelum trigger
MAX_WAIT = 12.0           # cap — paksa trigger kalau burst terlalu lama
EVENT_MAP = {
    "20965": "ONU_LOS",
    "20966": "ONU_OFFLINE",
    "20967": "ONU_ONLINE",
}


class ZTETrapProtocol(asyncio.DatagramProtocol):
    def __init__(self, sync_callback: Callable[[str], None]):
        self.sync_callback = sync_callback
        self.transport = None
        self._loop = None
        self._state = {}   # {olt_ip: {"burst_started_at": ..., "timer": ...}}

    def connection_made(self, transport):
        self.transport = transport
        self._loop = asyncio.get_running_loop()
        print(f"[TRAP-RECEIVER] Listening UDP port {LISTEN_PORT}")

    def datagram_received(self, data: bytes, addr):
        """JANGAN blocking di sini. Event loop utama."""
        # ⭐ RAW LOG — di paling atas, sebelum regex apa pun
        print(f"[RAW] from {addr} len={len(data)}: {data[:60].hex()}")
        try:
            self._handle_packet(data, addr)
        except Exception as e:
            # Jangan biarkan exception matikan receiver
            print(f"[TRAP-RECEIVER] Error packet dari {addr}: {e}")

    def _handle_packet(self, data: bytes, addr):
        raw_text = data.decode("latin-1", errors="ignore")

        # Extract eventid dari community string ZTE
        # Format: bagoes_ro@eventid=21010@eventLevel=major@confirm@20260917114255
        # Note: ada "@confirm@" opsional antara eventLevel dan timestamp
        # ⭐ Pakai IGNORECASE — payload ZTE pakai "eventId" (I kapital)
        m = re.search(r"eventid=(\d+)@eventlevel=(\w+)", raw_text, re.IGNORECASE)
        if not m:
            print(f"[TRAP] Regex TIDAK match. Raw: {raw_text[:150]!r}")
            return

        event_id = m.group(1)
        event_level = m.group(2)
        # Extract timestamp setelah "@" (4th field atau last)
        ts_match = re.search(r"@(\d{14})", raw_text)
        ts_olt = ts_match.group(1) if ts_match else "unknown"
        olt_ip = addr[0]
        event_name = EVENT_MAP.get(event_id, f"UNKNOWN_{event_id}")

        print(f"[TRAP] {olt_ip} → {event_id} ({event_name}) @ {ts_olt}")

        # Debounce + trigger sync
        self._schedule_sync(olt_ip)

    def _schedule_sync(self, olt_ip: str):
        now = time.time()
        state = self._state.get(olt_ip, {"burst_started_at": None, "timer": None})

        # ⭐ WAJIB: cancel timer lama sebelum set baru (disiplin)
        if state["timer"] is not None:
            state["timer"].cancel()

        # Set burst_started_at kalau baru mulai
        if state["burst_started_at"] is None:
            state["burst_started_at"] = now

        # ⭐ MAX_WAIT cap — paksa trigger kalau burst terlalu lama
        elapsed_burst = now - state["burst_started_at"]
        if elapsed_burst >= MAX_WAIT:
            print(f"[TRAP] {olt_ip} burst >{MAX_WAIT}s — force trigger")
            state["burst_started_at"] = None
            state["timer"] = None
            self._state[olt_ip] = state
            self._fire_sync(olt_ip)
            return

        # Set timer debounce
        state["timer"] = self._loop.call_later(
            DEBOUNCE_WINDOW, self._on_timer_fire, olt_ip
        )
        self._state[olt_ip] = state

    def _on_timer_fire(self, olt_ip: str):
        state = self._state.get(olt_ip)
        if state:
            state["burst_started_at"] = None
            state["timer"] = None
        print(f"[TRAP] {olt_ip} — debounce selesai, trigger sync")
        self._fire_sync(olt_ip)

    def _fire_sync(self, olt_ip: str):
        try:
            self.sync_callback(olt_ip)
        except Exception as e:
            print(f"[TRAP-SYNC] Error: {e}")


async def start_trap_receiver(sync_callback) -> Optional[object]:
    """Start trap receiver. Return transport (untuk shutdown) atau None."""
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: ZTETrapProtocol(sync_callback),
            local_addr=("0.0.0.0", LISTEN_PORT),
        )
        return transport
    except PermissionError:
        print(f"[TRAP-RECEIVER] Bind port {LISTEN_PORT} gagal (perlu sudo?)")
        return None
    except Exception as e:
        print(f"[TRAP-RECEIVER] Error: {e}")
        return None
