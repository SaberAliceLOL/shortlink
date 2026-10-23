"""Base62 编解码单测：边界值、往返一致性、随机样本、非法输入。"""

import random

import pytest

from app.services.base62 import BASE, BASE62_ALPHABET, decode, encode


class TestEncodeBoundaries:
    def test_zero(self) -> None:
        assert encode(0) == "0"

    def test_base_boundary(self) -> None:
        # 61 → 'z'（最后一位），62 → '10'（进位）
        assert encode(BASE - 1) == BASE62_ALPHABET[-1]
        assert encode(BASE) == "10"

    def test_id_start_is_5_chars(self) -> None:
        # 生产 ID 起点 62^4 = 14776336 → 编码后正好 5 位
        assert encode(62**4) == "10000"
        assert len(encode(62**4)) == 5

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            encode(-1)


class TestRoundTrip:
    @pytest.mark.parametrize("n", [0, 1, 61, 62, 3844, 14776335, 14776336, 10**12])
    def test_known_values(self, n: int) -> None:
        assert decode(encode(n)) == n

    def test_random_1000(self) -> None:
        rng = random.Random(42)
        for _ in range(1000):
            n = rng.randint(0, 62**7)
            assert decode(encode(n)) == n

    def test_length_bounds(self) -> None:
        # 62^7 - 1 是 7 位短码的上限（CHAR(7) 的设计依据）
        assert len(encode(62**7 - 1)) == 7


class TestDecodeErrors:
    def test_illegal_char(self) -> None:
        with pytest.raises(ValueError, match="非法"):
            decode("abc-1")

    def test_empty(self) -> None:
        assert decode("") == 0  # 空串按 0 处理（幂等解码）
