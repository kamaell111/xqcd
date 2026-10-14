"""
Test untuk profile_driver_c320 — tanpa OLT, tanpa DB.
Jalankan dari folder backend:   pytest test_profile_driver.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from profile_driver_c320 import (  # noqa: E402
    ParseError,
    build_tcont_create,
    build_tcont_delete,
    build_traffic_create,
    build_traffic_delete,
    parse,
    parse_tcont,
    parse_traffic,
)

# Sampel persis dari output OLT (termasuk spasi di akhir baris)
TCONT_SAMPLE = (
    "Profile name :default  \n"
    " Type    FBW(kbps)  ABW(kbps)  MBW(kbps)  PRIORITY  WEIGHT    \n"
    " 1       10000      0          0          N/A       N/A         \n"
    "\n"
    "Profile name :1G  \n"
    " Type    FBW(kbps)  ABW(kbps)  MBW(kbps)  PRIORITY  WEIGHT    \n"
    " 4       0          0          1000000    N/A       N/A         \n"
)

TRAFFIC_SAMPLE = (
    "Profile name  :default          \n"
    "  SIR(kbps)   PIR(kbps)   CBS(kbytes)  PBS(kbytes)          \n"
    "  9953280     9953280     default      default             \n"
    "\n"
    "Profile name  :1G          \n"
    "  SIR(kbps)   PIR(kbps)   CBS(kbytes)  PBS(kbytes)          \n"
    "  1000000     1000000     default      default             \n"
)


# ---------------------------------------------------------------- T-CONT
def test_tcont_sampel_asli():
    r = parse_tcont(TCONT_SAMPLE)
    assert [x["name"] for x in r] == ["default", "1G"]
    assert r[0] == {
        "name": "default", "type": 1, "fixed": 10000, "assured": 0,
        "maximum": 0, "priority": None, "weight": None,
    }
    assert r[1]["type"] == 4
    assert r[1]["maximum"] == 1000000
    assert r[1]["fixed"] == 0


def test_tcont_priority_dan_weight_angka():
    txt = (
        "Profile name :P1\n"
        " Type FBW(kbps) ABW(kbps) MBW(kbps) PRIORITY WEIGHT\n"
        " 4    0         0         50000     2        5\n"
    )
    r = parse_tcont(txt)[0]
    assert r["priority"] == 2 and r["weight"] == 5 and r["maximum"] == 50000


def test_tcont_crlf_ansi_dan_backspace():
    txt = TCONT_SAMPLE.replace("\n", "\r\n")
    txt = "\x1b[0m" + txt.replace("1G", "1\x08" + "1G")
    # "1\x081G" -> setelah backspace dibuang jadi "11G"; yang penting tidak error
    r = parse_tcont(txt)
    assert len(r) == 2


def test_tcont_dengan_echo_command_dan_prompt():
    txt = "ZXAN#show gpon profile tcont\n" + TCONT_SAMPLE + "\nZXAN#"
    r = parse_tcont(txt)
    assert [x["name"] for x in r] == ["default", "1G"]


def test_tcont_nama_dengan_spasi_dan_titik_dua():
    txt = (
        "Profile name :My Plan:10\n"
        " Type FBW(kbps) ABW(kbps) MBW(kbps) PRIORITY WEIGHT\n"
        " 2    0         2000      0         N/A      N/A\n"
    )
    r = parse_tcont(txt)
    assert r[0]["name"] == "My Plan:10"
    assert r[0]["assured"] == 2000


def test_tcont_urutan_kolom_dibaca_dari_header():
    # header dibalik: parser tidak boleh bergantung pada urutan tetap
    txt = (
        "Profile name :X\n"
        " MBW(kbps) ABW(kbps) FBW(kbps) Type\n"
        " 700       0         0         4\n"
    )
    r = parse_tcont(txt)[0]
    assert r["maximum"] == 700 and r["type"] == 4 and r["fixed"] == 0


def test_tcont_banyak_profile():
    blok = (
        "Profile name :P{i}\n"
        " Type FBW(kbps) ABW(kbps) MBW(kbps) PRIORITY WEIGHT\n"
        " 4    0         0         {i}000    N/A      N/A\n"
    )
    txt = "".join(blok.format(i=i) for i in range(1, 31))
    r = parse_tcont(txt)
    assert len(r) == 30 and r[29]["maximum"] == 30000


# --------------------------------------------------------------- Traffic
def test_traffic_sampel_asli():
    r = parse_traffic(TRAFFIC_SAMPLE)
    assert r[0] == {
        "name": "default", "sir": 9953280, "pir": 9953280,
        "cbs": "default", "pbs": "default",
    }
    assert r[1]["name"] == "1G" and r[1]["sir"] == 1000000


def test_traffic_cbs_pbs_angka():
    txt = (
        "Profile name :B\n"
        " SIR(kbps) PIR(kbps) CBS(kbytes) PBS(kbytes)\n"
        " 1000      2000      100          200\n"
    )
    r = parse_traffic(txt)[0]
    assert r["cbs"] == 100 and r["pbs"] == 200


# ------------------------------------------------- Pengaman / kasus buruk
def test_paging_more_ditolak():
    txt = TCONT_SAMPLE + "\n--More--\x08\x08\x08        "
    with pytest.raises(ParseError):
        parse_tcont(txt)


def test_sampah_ditolak():
    with pytest.raises(ParseError):
        parse_tcont("lorem ipsum dolor sit amet")


def test_string_kosong_dan_none_ditolak():
    with pytest.raises(ParseError):
        parse_tcont("")
    with pytest.raises(ParseError):
        parse_traffic(None)


def test_error_cli_ditolak():
    with pytest.raises(ParseError):
        parse_tcont("% Invalid input detected at '^' marker.")


def test_pesan_kosong_dikenali_boleh_list_kosong():
    assert parse_tcont("No related information to show.\n") == []


def test_jumlah_kolom_tidak_cocok_ditolak():
    txt = (
        "Profile name :X\n"
        " Type FBW(kbps) ABW(kbps) MBW(kbps) PRIORITY WEIGHT\n"
        " 4    0         0\n"
    )
    with pytest.raises(ParseError):
        parse_tcont(txt)


def test_header_tanpa_data_ditolak():
    txt = (
        "Profile name :X\n"
        " Type FBW(kbps) ABW(kbps) MBW(kbps) PRIORITY WEIGHT\n"
    )
    with pytest.raises(ParseError):
        parse_tcont(txt)


def test_nama_ganda_ditolak():
    with pytest.raises(ParseError):
        parse_tcont(TCONT_SAMPLE + "\n" + TCONT_SAMPLE)


def test_type_bukan_angka_ditolak():
    txt = (
        "Profile name :X\n"
        " Type FBW(kbps) ABW(kbps) MBW(kbps) PRIORITY WEIGHT\n"
        " abc  0         0         0         N/A      N/A\n"
    )
    with pytest.raises(ParseError):
        parse_tcont(txt)


def test_dispatch_parse():
    assert len(parse("tcont", TCONT_SAMPLE)) == 2
    assert len(parse("traffic", TRAFFIC_SAMPLE)) == 2
    with pytest.raises(ValueError):
        parse("onu", "x")


# ----------------------------------------------------------- Builder T-CONT
def test_build_tcont_type1():
    assert build_tcont_create("F10M", 1, fixed=10000) == [
        "profile tcont F10M type 1 fixed 10000"
    ]


def test_build_tcont_type2():
    assert build_tcont_create("A5M", 2, assured=5000) == [
        "profile tcont A5M type 2 assured 5000"
    ]


def test_build_tcont_type4_tanpa_dan_dengan_priority():
    assert build_tcont_create("100M", 4, maximum=100000) == [
        "profile tcont 100M type 4 maximum 100000"
    ]
    assert build_tcont_create("100M", 4, maximum=100000, priority=3) == [
        "profile tcont 100M type 4 maximum 100000 priority 3"
    ]


def test_build_tcont_batas_rentang():
    assert build_tcont_create("a", 4, maximum=64) == ["profile tcont a type 4 maximum 64"]
    assert build_tcont_create("a", 4, maximum=9953280)
    with pytest.raises(ValueError):
        build_tcont_create("a", 4, maximum=63)
    with pytest.raises(ValueError):
        build_tcont_create("a", 4, maximum=9953281)
    with pytest.raises(ValueError):
        build_tcont_create("a", 4, maximum=1000, priority=0)
    with pytest.raises(ValueError):
        build_tcont_create("a", 4, maximum=1000, priority=5)


def test_build_tcont_parameter_salah_untuk_type():
    with pytest.raises(ValueError):
        build_tcont_create("a", 1, assured=1000)          # type 1 butuh fixed
    with pytest.raises(ValueError):
        build_tcont_create("a", 1, fixed=1000, maximum=2000)
    with pytest.raises(ValueError):
        build_tcont_create("a", 2, assured=1000, priority=1)  # priority hanya type 4
    with pytest.raises(ValueError):
        build_tcont_create("a", 4)                         # maximum wajib


def test_build_tcont_type_3_5_dan_tak_valid_ditolak():
    with pytest.raises(ValueError, match="belum didukung"):
        build_tcont_create("a", 3, assured=1000)
    with pytest.raises(ValueError, match="belum didukung"):
        build_tcont_create("a", 5, fixed=1000)
    with pytest.raises(ValueError):
        build_tcont_create("a", 9, maximum=1000)
    with pytest.raises(ValueError):
        build_tcont_create("a", True, maximum=1000)


def test_build_tipe_data_ketat():
    with pytest.raises(ValueError):
        build_tcont_create("a", 4, maximum="1000")
    with pytest.raises(ValueError):
        build_tcont_create("a", 4, maximum=1000.5)
    with pytest.raises(ValueError):
        build_tcont_create("a", 4, maximum=True)


# ------------------------------------------------------------ Builder nama
def test_nama_valid_dan_tidak_valid():
    assert build_tcont_delete("Paket_10M.v2-a") == ["no profile tcont Paket_10M.v2-a"]
    assert build_traffic_delete("1G") == ["no profile traffic 1G"]
    assert build_tcont_delete("a" * 64)
    for buruk in ["", "a" * 65, "ada spasi", "x;reload", "a\nb", "abc\n", "a/b", "né", None, 5]:
        with pytest.raises(ValueError):
            build_tcont_delete(buruk)
        with pytest.raises(ValueError):
            build_traffic_delete(buruk)


def test_injeksi_lewat_nama_diblok_di_semua_builder():
    with pytest.raises(ValueError):
        build_tcont_create("x\nend", 4, maximum=1000)
    with pytest.raises(ValueError):
        build_traffic_create("x\nend", 1000)


# ---------------------------------------------------------- Builder Traffic
def test_build_traffic_minimal_dan_lengkap():
    assert build_traffic_create("U", 1000) == ["profile traffic U sir 1000"]
    assert build_traffic_create("U", 1000, pir=2000, cbs=10, pbs=20) == [
        "profile traffic U sir 1000 pir 2000 cbs 10 pbs 20"
    ]
    assert build_traffic_create("U", 0) == ["profile traffic U sir 0"]


def test_build_traffic_rentang():
    assert build_traffic_create("U", 10000000, pir=10000000)
    with pytest.raises(ValueError):
        build_traffic_create("U", 10000001)
    with pytest.raises(ValueError):
        build_traffic_create("U", -1)
    with pytest.raises(ValueError):
        build_traffic_create("U", 1000, pir=63)
    with pytest.raises(ValueError):
        build_traffic_create("U", 1000, cbs=1024)
    with pytest.raises(ValueError):
        build_traffic_create("U", 1000, pbs=-1)
    with pytest.raises(ValueError):
        build_traffic_create("U", "1000")


def test_roundtrip_builder_bukan_parser_tetap_terpisah():
    # builder hanya menghasilkan list[str] satu baris di config-gpon
    out = build_tcont_create("100M", 4, maximum=100000)
    assert isinstance(out, list) and all(isinstance(x, str) for x in out)
    assert not any(x.startswith(("configure", "gpon", "end")) for x in out)
