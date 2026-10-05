"""Erasure recovery tests independent of the member-file storage engine."""

from itertools import combinations

import numpy as np
import pytest

from raidiant.codec import ReedSolomon
import raidiant.codec as codec_module


def random_data(codec, size=509, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.integers(0, 256, size, dtype=np.uint8).tobytes() for _ in range(codec.k)]


@pytest.mark.parametrize("n,m", [(3, 1), (4, 1), (4, 2), (5, 2), (6, 3), (7, 4), (8, 4)])
def test_every_tolerated_failure_combination(n, m):
    codec = ReedSolomon(n, m)
    data = random_data(codec)
    encoded = codec.encode(data)
    assert encoded[: codec.k] == data
    assert encoded[codec.k] == np.bitwise_xor.reduce(
        np.array([np.frombuffer(shard, dtype=np.uint8) for shard in data]), axis=0
    ).tobytes()
    for missing_count in range(m + 1):
        for missing in combinations(range(n), missing_count):
            surviving = {index: value for index, value in enumerate(encoded) if index not in missing}
            assert codec.reconstruct(surviving) == encoded


@pytest.mark.parametrize("n,m", [(12, 2), (16, 6), (32, 1), (32, 12), (32, 30)])
def test_larger_arrays_at_failure_limit(n, m):
    codec = ReedSolomon(n, m)
    encoded = codec.encode(random_data(codec, size=257, seed=1234))
    rng = np.random.default_rng(9876)
    for _ in range(30):
        available = rng.choice(n, codec.k, replace=False)
        assert codec.reconstruct({int(index): encoded[int(index)] for index in available}) == encoded


def gf_multiply_reference(left, right):
    # Deliberately independent scalar implementation for format golden vectors.
    result = 0
    while right:
        if right & 1:
            result ^= left
        left <<= 1
        if left & 0x100:
            left ^= 0x11D
        right >>= 1
    return result


def test_known_vector_and_field_arithmetic():
    codec = ReedSolomon(4, 2)
    left = bytes([0, 1, 2, 255, 128, 32])
    right = bytes([255, 2, 1, 0, 127, 16])
    # Normalized rows [3,2] and [2,3] scale to [1,1] and [2/3,3/2].
    # In this field 2/3 = 0xf5 and 3/2 = 0x8f.
    parity0 = bytes(a ^ b for a, b in zip(left, right))
    parity1 = bytes(gf_multiply_reference(0xF5, a) ^ gf_multiply_reference(0x8F, b) for a, b in zip(left, right))
    encoded = codec.encode([left, right])
    assert codec_module.CODEC_VERSION == "rs-gf256-v1"
    assert encoded == [left, right, parity0, parity1]
    assert codec.reconstruct({2: parity0, 3: parity1}) == encoded
    for left_value in range(256):
        for right_value in [0, 1, 2, 127, 128, 255]:
            assert int(codec_module._MULTIPLY[left_value, right_value]) == gf_multiply_reference(left_value, right_value)


@pytest.mark.parametrize("n,m", [(2, 1), (33, 1), (3, 0), (3, 2), (3, -1)])
def test_invalid_geometry(n, m):
    with pytest.raises(ValueError):
        ReedSolomon(n, m)


@pytest.mark.parametrize("n,m", [(True, 1), (3, True), (3.0, 1), (3, "1")])
def test_noninteger_geometry(n, m):
    with pytest.raises(TypeError):
        ReedSolomon(n, m)


def test_invalid_shards():
    codec = ReedSolomon(5, 2)
    with pytest.raises(ValueError, match="Expected"):
        codec.encode([b"x"])
    with pytest.raises(ValueError, match="same length"):
        codec.encode([b"x", b"yy", b"z"])
    with pytest.raises(TypeError, match="immutable bytes"):
        codec.encode([b"x", bytearray(b"x"), b"x"])
    with pytest.raises(ValueError, match="At least"):
        codec.reconstruct({0: b"x", 1: b"y"})
    for invalid_index in [-1, 5, True, "2", 2.0]:
        with pytest.raises(ValueError, match="indices"):
            codec.reconstruct({3: b"x", 4: b"y", invalid_index: b"z"})
    # Extras cannot evade validation just because the first k are usable.
    with pytest.raises(ValueError, match="same length"):
        codec.reconstruct({0: b"x", 1: b"y", 2: b"z", 4: b""})
    with pytest.raises(TypeError, match="immutable bytes"):
        codec.reconstruct({0: b"x", 1: b"y", 2: b"z", 4: None})
    with pytest.raises(TypeError, match="mapping"):
        codec.reconstruct([b"x", b"y", b"z"])


def test_zero_length_shards():
    codec = ReedSolomon(6, 2)
    assert codec.encode([b""] * codec.k) == [b""] * codec.n
    assert codec.reconstruct({2: b"", 3: b"", 4: b"", 5: b""}) == [b""] * codec.n


def test_matrix_cache_has_fixed_limit():
    codec = ReedSolomon(12, 6)
    encoded = codec.encode(random_data(codec, size=19))
    for available in list(combinations(range(codec.n), codec.k))[1:101]:
        assert codec.reconstruct({index: encoded[index] for index in available}) == encoded
    assert len(codec._decode_cache) == 64
    assert all(matrix.nbytes == codec.k * codec.k for matrix in codec._decode_cache.values())


def test_multiplication_scratch_is_bounded_and_chunk_boundaries_work(monkeypatch):
    codec = ReedSolomon(6, 3)
    # Small chunks exercise the same boundaries cheaply and deterministically.
    monkeypatch.setattr(codec_module, "_WORK_BYTES", 127)
    calls = []
    original_take = np.take

    def checked_take(table, indices, **kwargs):
        calls.append(indices.nbytes)
        assert indices.nbytes <= 127
        assert kwargs["out"].nbytes <= 127
        assert kwargs["mode"] == "clip"
        return original_take(table, indices, **kwargs)

    monkeypatch.setattr(codec_module.np, "take", checked_take)
    data = random_data(codec, size=1027)
    encoded = codec.encode(data)
    assert codec.reconstruct({3: encoded[3], 4: encoded[4], 5: encoded[5]}) == encoded
    assert len(calls) > 8
