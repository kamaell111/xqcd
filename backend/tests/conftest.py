"""Pytest config — tambahkan backend/ ke sys.path biar import absolut jalan.
Test files pakai pola: `import vlanif_core as c` (di backend root).
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
