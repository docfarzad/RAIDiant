"""Validate shipping icon containers and preserve the supplied source artwork."""
import hashlib
import struct
import zlib

from raidiant.branding import ASSETS


def icns_chunks(name):
    data = (ASSETS / name).read_bytes()
    assert data[:4] == b"icns"
    assert struct.unpack(">I", data[4:8])[0] == len(data)
    chunks, offset = {}, 8
    while offset < len(data):
        kind, size = struct.unpack(">4sI", data[offset:offset + 8])
        assert size >= 8 and offset + size <= len(data)
        assert kind not in chunks
        chunks[kind] = data[offset + 8:offset + size]
        offset += size
    assert offset == len(data)
    return chunks


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset, compressed, ended = 8, bytearray(), False
    while offset < len(data):
        size, kind = struct.unpack(">I4s", data[offset:offset + 8])
        payload = data[offset + 8:offset + 8 + size]
        crc = struct.unpack(">I", data[offset + 8 + size:offset + 12 + size])[0]
        assert zlib.crc32(kind + payload) == crc
        if kind == b"IDAT":
            compressed.extend(payload)
        if kind == b"IEND":
            ended = True
        offset += size + 12
    assert ended and offset == len(data)
    assert zlib.decompress(compressed)
    return struct.unpack(">II", data[16:24])


def test_macos_icon_has_all_standard_and_retina_slots():
    expected = {b"ic07": 128,
                b"ic08": 256, b"ic09": 512, b"ic11": 32, b"ic12": 64,
                b"ic13": 256, b"ic14": 512, b"ic10": 1024}
    chunks = icns_chunks("app-icon.icns")
    assert set(chunks) == set(expected) | {b"ic04", b"ic05", b"info"}
    # The supplied Apple container uses compressed ARGB for 16/32 at 1x.
    assert chunks[b"ic04"].startswith(b"ARGB")
    assert chunks[b"ic05"].startswith(b"ARGB")
    assert chunks[b"info"].startswith(b"bplist00")
    for kind, size in expected.items():
        assert png_size(chunks[kind]) == (size, size)
        assert chunks[kind] == (ASSETS / f"app-icon-{size}.png").read_bytes()


def test_source_art_and_existing_representations_are_preserved():
    source = (ASSETS / "app-icon.icns").read_bytes()
    assert hashlib.sha256(source).hexdigest() == "4163d7bdb598c5cb98bd4e8a84458f91265454fed06dd4928b2e12aaeb03afe5"
    # Source frames from RAIDiant Icons.zip are copied, not rerendered.
    expected = {
        16: "10900c4a9ba623bdefd002f209631760cec02dad06439fe701f74d1ac32dea2b",
        32: "d679ee3f72e43b304f05ab00994407262f07d2c5d8bd4846d376cfcaca8d5089",
        64: "d35e998de28e41a3dcd91f8c9f340ba3da523ec3ccff022f443454465a7c21de",
        128: "4dcd22b2e35bd43ca2e408653636f72846d5de370e5d7ecb571a3dd9c43f1ddb",
        256: "0dfc965f6f784e47b71b8a2a1dfaef0dd9701b092591230adb47e47632d0354e",
        512: "2f6fcfba54766dd07611e60228b9c33451b03a5f025e9329d8f88a930d10bf85",
        1024: "facbdb605b8b81b88db33e4833a6d40b7c37c025bc255d39e074b999094df163",
        48: "6037e86ce93605567554090be373097a9a6315c7c313018e7c703d29e477ae10",
        72: "0d731eb6ef70196e69fe01b9cdcb2c6767693d2cca89de7b469d2a350d4387d3",
        96: "26f3781019bc79dbf906975e7d8426d8e025e7dc785515c639d554c1cdf463a9",
        192: "e831dba0c6ace4e121a3cde1f34b3b9d77e893bd9c9b8b08b9ad8a247dfd2205",
    }
    for size, digest in expected.items():
        png = (ASSETS / f"app-icon-{size}.png").read_bytes()
        assert png_size(png) == (size, size)
        assert hashlib.sha256(png).hexdigest() == digest


def test_windows_icon_has_list_dpi_and_jumbo_representations():
    sizes = (16, 20, 24, 32, 40, 48, 64, 72, 96, 128, 160, 192, 256)
    data = (ASSETS / "app-icon.ico").read_bytes()
    assert struct.unpack("<HHH", data[:6]) == (0, 1, len(sizes))
    next_offset = 6 + 16 * len(sizes)
    for index, size in enumerate(sizes):
        width, height, colors, reserved, planes, bits, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * index:22 + 16 * index])
        assert (width or 256, height or 256, colors, reserved, planes, bits) == (size, size, 0, 0, 1, 32)
        assert offset == next_offset
        png = data[offset:offset + length]
        assert png_size(png) == (size, size)
        assert png == (ASSETS / f"app-icon-{size}.png").read_bytes()
        next_offset += length
    assert next_offset == len(data)


def test_linux_launcher_matches_executable_and_window_class():
    launcher = (ASSETS / "RAIDiant.desktop").read_text(encoding="utf-8")
    for line in ("Type=Application", "Exec=RAIDiant", "Icon=raidiant", "Terminal=false", "StartupWMClass=RAIDiant"):
        assert line in launcher.splitlines()
    assert png_size((ASSETS / "app-icon-512.png").read_bytes()) == (512, 512)
