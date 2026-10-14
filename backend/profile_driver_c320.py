"""
profile_driver_c320.py — driver profile GPON untuk ZTE C320 (V2.1.0)

Isi: parser output `show gpon profile ...` dan command builder create/delete.
SEMUA fungsi di sini MURNI: tidak menyentuh OLT, DB, maupun jaringan,
sehingga bisa diuji penuh dengan pytest.

Multi-OLT: router memilih driver per model OLT. Model lain (C300, C600, ...)
cukup membuat modul driver baru dengan antarmuka yang sama:
    KINDS, SHOW_COMMANDS, ParseError, parse(kind, text)
    + builder (dipakai Fase 3).

Prinsip keselamatan parser: lebih baik GAGAL (ParseError) daripada
mengembalikan daftar parsial. Router tidak akan menyentuh cache DB kalau
parser melempar ParseError.
"""
import re
from typing import Any, Dict, List, Optional

MODEL = "ZTE-C320"
KINDS = ("tcont", "traffic")
SHOW_COMMANDS = {
    "tcont": "show gpon profile tcont",
    "traffic": "show gpon profile traffic",
}


class ParseError(ValueError):
    """Output OLT tidak bisa dipercaya / tidak sesuai format yang dikenal."""


# --------------------------------------------------------------------------
# PARSER
# --------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MORE_RE = re.compile(r"-{2,}\s*more\s*-{2,}", re.I)
_NAME_LINE_RE = re.compile(r"^profile\s+name\s*:\s*(.*)$", re.I)
_EMPTY_RE = re.compile(r"no\s+related\s+information|no\s+profile", re.I)
_ERROR_RE = re.compile(
    r"%\s*(invalid|incomplete|unknown|ambiguous)|invalid\s+input|"
    r"unrecognized\s+command|\berror\b",
    re.I,
)


def _normalize(text: Optional[str]) -> str:
    """Bersihkan kode ANSI, backspace, dan newline gaya Windows."""
    if text is None:
        return ""
    t = _ANSI_RE.sub("", text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\x08", "")
    return t


def _col(token: str) -> str:
    """'FBW(kbps)' -> 'fbw', 'PRIORITY' -> 'priority'."""
    return re.sub(r"\(.*?\)", "", token).strip().lower()


def _val(token: str) -> Any:
    """Angka -> int, N/A -> None, lainnya (mis. 'default') -> string lowercase."""
    if re.fullmatch(r"\d+", token):
        return int(token)
    if token.upper() in ("N/A", "NA", "-"):
        return None
    return token.lower()


def _parse_blocks(text: Optional[str], header_required: set) -> List[Dict[str, Any]]:
    """
    Mesin status per baris:
      'Profile name :X'  -> mulai record baru
      baris header       -> urutan kolom dibaca dari sini (tidak di-hardcode)
      baris data         -> di-zip dengan header
    Baris lain (echo command, prompt) diabaikan.
    """
    t = _normalize(text)
    if _MORE_RE.search(t):
        raise ParseError("output terpotong paging (--More--); daftar tidak lengkap")

    records: List[Dict[str, Any]] = []
    st: Optional[Dict[str, Any]] = None

    def flush() -> None:
        nonlocal st
        if st is None:
            return
        if st["header"] is None or st["row"] is None:
            raise ParseError(f"profile '{st['name']}': header/data tidak lengkap")
        rec: Dict[str, Any] = {"name": st["name"]}
        rec.update(st["row"])
        records.append(rec)
        st = None

    for line in t.split("\n"):
        s = line.strip()
        if not s:
            continue
        m = _NAME_LINE_RE.match(s)
        if m:
            flush()
            name = m.group(1).strip()
            if not name:
                raise ParseError("nama profile kosong")
            st = {"name": name, "header": None, "row": None}
            continue
        if st is None or st["row"] is not None:
            continue  # echo/prompt di luar record, atau baris sisa setelah data
        toks = s.split()
        if st["header"] is None:
            cols = [_col(x) for x in toks]
            if header_required <= set(cols):
                st["header"] = cols
            continue
        if len(toks) != len(st["header"]):
            raise ParseError(
                f"profile '{st['name']}': jumlah kolom data ({len(toks)}) "
                f"!= header ({len(st['header'])})"
            )
        st["row"] = {c: _val(v) for c, v in zip(st["header"], toks)}
    flush()

    names = [r["name"] for r in records]
    if len(names) != len(set(names)):
        raise ParseError("nama profile ganda di output")
    return records


def _zero_records(text: Optional[str]) -> List[Dict[str, Any]]:
    """Kasus 0 record: hanya aman dianggap 'kosong' kalau OLT bilang begitu."""
    t = _normalize(text)
    if _ERROR_RE.search(t):
        raise ParseError("OLT menolak perintah: " + t.strip()[:120])
    if _EMPTY_RE.search(t):
        return []
    raise ParseError("0 profile terbaca dan output tidak dikenali sebagai 'kosong'")


_TCONT_MAP = {
    "type": "type",
    "fbw": "fixed",
    "abw": "assured",
    "mbw": "maximum",
    "priority": "priority",
    "weight": "weight",
}
_TRAFFIC_MAP = {"sir": "sir", "pir": "pir", "cbs": "cbs", "pbs": "pbs"}


def _remap(rec: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": rec["name"]}
    for k, v in rec.items():
        if k != "name":
            out[mapping.get(k, k)] = v
    return out


def parse_tcont(text: Optional[str]) -> List[Dict[str, Any]]:
    """-> [{name, type, fixed, assured, maximum, priority, weight}, ...] (kbps)"""
    recs = _parse_blocks(text, {"type", "fbw", "abw", "mbw"})
    if not recs:
        return _zero_records(text)
    out = [_remap(r, _TCONT_MAP) for r in recs]
    for o in out:
        if not isinstance(o.get("type"), int):
            raise ParseError(f"profile '{o['name']}': kolom Type bukan angka")
    return out


def parse_traffic(text: Optional[str]) -> List[Dict[str, Any]]:
    """-> [{name, sir, pir, cbs, pbs}, ...]; cbs/pbs bisa int atau 'default'"""
    recs = _parse_blocks(text, {"sir", "pir"})
    if not recs:
        return _zero_records(text)
    out = [_remap(r, _TRAFFIC_MAP) for r in recs]
    for o in out:
        if not isinstance(o.get("sir"), int) or not isinstance(o.get("pir"), int):
            raise ParseError(f"profile '{o['name']}': SIR/PIR bukan angka")
    return out


def parse(kind: str, text: Optional[str]) -> List[Dict[str, Any]]:
    if kind == "tcont":
        return parse_tcont(text)
    if kind == "traffic":
        return parse_traffic(text)
    raise ValueError(f"kind tidak dikenal: {kind!r}")


# --------------------------------------------------------------------------
# COMMAND BUILDER (dipakai Fase 3; ditaruh di sini agar kontraknya ikut teruji)
# Hasil = baris perintah di dalam mode `config-gpon` saja.
# `configure terminal`, `gpon`, dan `end` urusan executor.
# --------------------------------------------------------------------------
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # dipakai dgn fullmatch (bukan match + $)
BW_MIN, BW_MAX = 64, 9953280          # T-CONT, kbps
SIR_MIN, SIR_MAX = 0, 10000000        # Traffic, kbps
PIR_MIN, PIR_MAX = 64, 10000000
BURST_MIN, BURST_MAX = 0, 1023        # CBS / PBS, kbytes
PRIORITY_MIN, PRIORITY_MAX = 1, 4

# type -> (parameter wajib, parameter opsional). Hanya yang terbukti di probe.
_TCONT_RULES = {
    1: ("fixed", ()),
    2: ("assured", ()),
    4: ("maximum", ("priority",)),
}
_NOT_YET = (3, 5)


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(
            "nama profile harus 1-64 karakter dari huruf, angka, '_', '.', '-'"
        )
    return name


def _check_int(label: str, value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} harus integer")
    if not lo <= value <= hi:
        raise ValueError(f"{label} harus di antara {lo} dan {hi}")
    return value


def build_tcont_create(
    name: str,
    type: int,  # noqa: A002 - nama sesuai kontrak
    *,
    fixed: Optional[int] = None,
    assured: Optional[int] = None,
    maximum: Optional[int] = None,
    priority: Optional[int] = None,
) -> List[str]:
    _check_name(name)
    if isinstance(type, bool) or not isinstance(type, int):
        raise ValueError("type harus integer")
    if type in _NOT_YET:
        raise ValueError(f"T-CONT type {type} belum didukung")
    if type not in _TCONT_RULES:
        raise ValueError("T-CONT type harus 1, 2, atau 4")

    required, optional = _TCONT_RULES[type]
    given = {
        k: v
        for k, v in (
            ("fixed", fixed),
            ("assured", assured),
            ("maximum", maximum),
            ("priority", priority),
        )
        if v is not None
    }
    if required not in given:
        raise ValueError(f"type {type} wajib mengisi '{required}'")
    extra = set(given) - {required} - set(optional)
    if extra:
        raise ValueError(
            f"type {type} tidak boleh memakai: {', '.join(sorted(extra))}"
        )

    _check_int(required, given[required], BW_MIN, BW_MAX)
    cmd = f"profile tcont {name} type {type} {required} {given[required]}"
    if "priority" in given:
        _check_int("priority", given["priority"], PRIORITY_MIN, PRIORITY_MAX)
        cmd += f" priority {given['priority']}"
    return [cmd]


def build_traffic_create(
    name: str,
    sir: int,
    pir: Optional[int] = None,
    cbs: Optional[int] = None,
    pbs: Optional[int] = None,
) -> List[str]:
    _check_name(name)
    _check_int("sir", sir, SIR_MIN, SIR_MAX)
    cmd = f"profile traffic {name} sir {sir}"
    if pir is not None:
        _check_int("pir", pir, PIR_MIN, PIR_MAX)
        cmd += f" pir {pir}"
    if cbs is not None:
        _check_int("cbs", cbs, BURST_MIN, BURST_MAX)
        cmd += f" cbs {cbs}"
    if pbs is not None:
        _check_int("pbs", pbs, BURST_MIN, BURST_MAX)
        cmd += f" pbs {pbs}"
    return [cmd]


def build_tcont_delete(name: str) -> List[str]:
    return [f"no profile tcont {_check_name(name)}"]


def build_traffic_delete(name: str) -> List[str]:
    return [f"no profile traffic {_check_name(name)}"]

# --------------------------------------------------------------------------
# ANTARMUKA UNTUK profile_executor (Fase 3a). Modul ini dipakai sebagai `driver`.
# --------------------------------------------------------------------------
_CREATE_KEYS = {
    "tcont": {"name", "type", "fixed", "assured", "maximum", "priority"},
    "traffic": {"name", "sir", "pir", "cbs", "pbs"},
}


def is_reserved(name: Any) -> bool:
    """Nama bawaan OLT ('default') tidak boleh dibuat/dihapus. Tidak peka huruf besar/kecil."""
    return isinstance(name, str) and name.strip().lower() == "default"


def build_create(kind: str, params: Dict[str, Any]) -> List[str]:
    if kind not in _CREATE_KEYS:
        raise ValueError(f"kind tidak dikenal: {kind!r}")
    extra = set(params) - _CREATE_KEYS[kind]
    if extra:
        raise ValueError(f"parameter tidak berlaku untuk {kind}: {', '.join(sorted(extra))}")
    if kind == "tcont":
        return build_tcont_create(
            params.get("name"), params.get("type"),
            fixed=params.get("fixed"), assured=params.get("assured"),
            maximum=params.get("maximum"), priority=params.get("priority"))
    return build_traffic_create(
        params.get("name"), params.get("sir"),
        params.get("pir"), params.get("cbs"), params.get("pbs"))


def build_delete(kind: str, name: str) -> List[str]:
    if kind == "tcont":
        return build_tcont_delete(name)
    if kind == "traffic":
        return build_traffic_delete(name)
    raise ValueError(f"kind tidak dikenal: {kind!r}")


def matches_expected(kind: str, params: Dict[str, Any], item: Dict[str, Any]) -> bool:
    """Bandingkan HANYA field yang dikirim user; sisanya diisi default oleh OLT."""
    if kind == "tcont":
        required = _TCONT_RULES.get(params.get("type"), (None, ()))[0]
        keys = ["type"] + ([required] if required else [])
        if params.get("priority") is not None:
            keys.append("priority")
    elif kind == "traffic":
        keys = ["sir"] + [k for k in ("pir", "cbs", "pbs") if params.get(k) is not None]
    else:
        return False
    return all(item.get(k) == params.get(k) for k in keys)

