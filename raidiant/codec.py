"""Systematic Reed--Solomon erasure coding over GF(256).

The code uses the field polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11d)
and a normalized, scaled Vandermonde generator matrix. Any ``k = n - m`` distinct
shards recover the original data; the first k output shards are systematic
(unchanged input data), and the first parity shard is their literal XOR. This
is an erasure code, not an error detector: callers
must reject corrupt or stale shards before passing them to ``reconstruct``.

These parameters define an on-disk coding format. They must not be changed
without changing the storage format version. This code is not a Linux md,
hardware RAID, or other vendor's Reed--Solomon interchange format.

NumPy performs the byte operations. Auxiliary multiplication scratch space is
bounded independently of shard length, while the returned shards necessarily
occupy n * shard_length bytes. Storage engines should process fixed-size stripes
rather than passing entire member files to this module.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from functools import lru_cache

import numpy as np


_MAX_MEMBERS = 32
_INVERSE_CACHE_SIZE = 64
_WORK_BYTES = 1024 * 1024
FIELD_POLYNOMIAL = 0x11D
CODEC_VERSION = "rs-gf256-v1"


def _make_tables() -> tuple[np.ndarray, np.ndarray]:
    # Scalar arithmetic here is only for the tiny, constant field tables.
    exp = [0] * 510
    log = [0] * 256
    value = 1
    for power in range(255):
        exp[power] = value
        log[value] = power
        value <<= 1
        if value & 0x100:
            value ^= FIELD_POLYNOMIAL
    exp[255:] = exp[:255]
    multiply = np.zeros((256, 256), dtype=np.uint8)
    inverse = np.zeros(256, dtype=np.uint8)
    for left in range(1, 256):
        inverse[left] = exp[255 - log[left]]
        for right in range(1, 256):
            multiply[left, right] = exp[log[left] + log[right]]
    multiply.flags.writeable = False
    inverse.flags.writeable = False
    return multiply, inverse


_MULTIPLY, _INVERSE = _make_tables()


def _invert(matrix: np.ndarray) -> np.ndarray:
    """Gauss--Jordan inversion of a small square GF(256) matrix."""
    size = matrix.shape[0]
    augmented = np.concatenate(
        (matrix.copy(), np.eye(size, dtype=np.uint8)), axis=1
    )
    for column in range(size):
        pivot = next(
            (row for row in range(column, size) if augmented[row, column]),
            None,
        )
        if pivot is None:
            raise ValueError("Singular erasure-coding matrix")
        if pivot != column:
            augmented[[column, pivot]] = augmented[[pivot, column]]
        scale = int(_INVERSE[augmented[column, column]])
        augmented[column] = _MULTIPLY[scale, augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = int(augmented[row, column])
            if factor:
                augmented[row] ^= _MULTIPLY[factor, augmented[column]]
    result = augmented[:, size:].copy()
    result.flags.writeable = False
    return result


def _matrix_product(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    result = np.zeros((left.shape[0], right.shape[1]), dtype=np.uint8)
    for row in range(left.shape[0]):
        for column in range(left.shape[1]):
            factor = int(left[row, column])
            if factor:
                result[row] ^= _MULTIPLY[factor, right[column]]
    return result


@lru_cache(maxsize=64)
def _generator(n: int, k: int) -> np.ndarray:
    vandermonde = np.ones((n, k), dtype=np.uint8)
    for row in range(n):
        for column in range(1, k):
            vandermonde[row, column] = _MULTIPLY[
                vandermonde[row, column - 1], row
            ]
    # Normalizing just the top rows, or appending identity rows to arbitrary
    # parity rows, does not in general give an MDS code. Normalize all rows.
    matrix = _matrix_product(vandermonde, _invert(vandermonde[:k]))
    # Scale each column by the inverse of its coefficient in the first parity
    # row. Compensating row scaling restores the systematic identity rows.
    # Nonzero row/column scaling preserves every invertible k-row submatrix,
    # hence the MDS property, while making the first parity literal XOR.
    column_scale = _INVERSE[matrix[k]].copy()
    for column in range(k):
        matrix[:, column] = _MULTIPLY[column_scale[column], matrix[:, column]]
    matrix[:k] = np.eye(k, dtype=np.uint8)
    matrix.flags.writeable = False
    return matrix


def _linear_combination(coefficients: np.ndarray, shards: Sequence[bytes]) -> bytes:
    size = len(shards[0])
    if not size:
        return b""
    nonzero = np.flatnonzero(coefficients)
    if len(nonzero) == 1 and coefficients[nonzero[0]] == 1:
        return shards[int(nonzero[0])]
    output = np.zeros(size, dtype=np.uint8)
    scratch = np.empty(min(size, _WORK_BYTES), dtype=np.uint8)
    for index in nonzero:
        source = np.frombuffer(shards[int(index)], dtype=np.uint8)
        factor = int(coefficients[index])
        if factor == 1:
            np.bitwise_xor(output, source, out=output)
            continue
        for offset in range(0, size, _WORK_BYTES):
            end = min(offset + _WORK_BYTES, size)
            work = scratch[: end - offset]
            # All inputs are uint8; clipping cannot alter an index. Unlike
            # mode='raise', this avoids NumPy's full temporary output buffer.
            np.take(_MULTIPLY[factor], source[offset:end], out=work, mode="clip")
            np.bitwise_xor(output[offset:end], work, out=output[offset:end])
    return output.tobytes()


def _validate_shards(shards: Sequence[bytes]) -> None:
    if any(not isinstance(shard, bytes) for shard in shards):
        raise TypeError("Shards must be immutable bytes objects")
    if any(len(shard) != len(shards[0]) for shard in shards):
        raise ValueError("All shards must have the same length")


class ReedSolomon:
    """Encode n total members with m parity shards and k data shards.

    Supported arrays contain 3 through 32 total members, at least one parity
    member and at least two data members. At most 64 decoding matrices are
    cached per instance. Instances should be confined to one worker thread.
    """

    def __init__(self, n: int, m: int) -> None:
        if type(n) is not int or type(m) is not int:
            raise TypeError("Member and parity counts must be integers")
        if not 3 <= n <= _MAX_MEMBERS:
            raise ValueError("Total member count must be between 3 and 32")
        if not 1 <= m <= n - 2:
            raise ValueError("Parity count must leave at least two data members")
        self.n = n
        self.m = m
        self.k = n - m
        self._matrix = _generator(n, self.k)
        self._decode_cache: OrderedDict[tuple[int, ...], np.ndarray] = OrderedDict()

    def encode(self, data: list[bytes]) -> list[bytes]:
        """Return all n shards from exactly k equal-length data shards."""
        if len(data) != self.k:
            raise ValueError(f"Expected {self.k} data shards, got {len(data)}")
        _validate_shards(data)
        return list(data) + [
            _linear_combination(self._matrix[index], data)
            for index in range(self.k, self.n)
        ]

    def reconstruct(self, shards: dict[int, bytes]) -> list[bytes]:
        """Reconstruct all n shards from at least k trustworthy indexed shards.

        Indices are logical coding positions (0 through n-1), before any
        physical stripe rotation. All supplied shards are validated, including
        unused extras. No silent-error correction or checksum checking occurs.
        """
        if not isinstance(shards, Mapping):
            raise TypeError("Shards must be a mapping of index to bytes")
        if any(type(index) is not int or not 0 <= index < self.n for index in shards):
            raise ValueError(f"Shard indices must be integers from 0 to {self.n - 1}")
        if len(shards) < self.k:
            raise ValueError(f"At least {self.k} distinct shards are required")
        _validate_shards(list(shards.values()))
        selected = tuple(sorted(shards)[: self.k])
        if selected == tuple(range(self.k)):
            return self.encode([shards[index] for index in selected])
        decoding = self._decode_cache.get(selected)
        if decoding is None:
            decoding = _invert(self._matrix[list(selected)])
            self._decode_cache[selected] = decoding
            if len(self._decode_cache) > _INVERSE_CACHE_SIZE:
                self._decode_cache.popitem(last=False)
        else:
            self._decode_cache.move_to_end(selected)
        chosen_shards = [shards[index] for index in selected]
        data = [
            shards[index]
            if index in shards
            else _linear_combination(decoding[index], chosen_shards)
            for index in range(self.k)
        ]
        return self.encode(data)
