"""Base62 编解码：ID ↔ 短码。

字符表顺序即进位权重（这里是 0-9A-Za-z）。
ID 起点 62^4，所以生产路径上短码恒为 5-7 位（62^7 ≈ 3.5 万亿）。
"""

BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(BASE62_ALPHABET)


def encode(num: int) -> str:
    """非负整数 → Base62 字符串。0 → '0'。"""
    if num < 0:
        raise ValueError("encode 只接受非负整数")
    if num == 0:
        return BASE62_ALPHABET[0]
    chars: list[str] = []
    while num > 0:
        num, rem = divmod(num, BASE)
        chars.append(BASE62_ALPHABET[rem])
    return "".join(reversed(chars))


def decode(code: str) -> int:
    """Base62 字符串 → 非负整数。含非法字符时抛 ValueError。"""
    num = 0
    for ch in code:
        v = BASE62_ALPHABET.find(ch)
        if v < 0:
            raise ValueError(f"非法 Base62 字符: {ch!r}")
        num = num * BASE + v
    return num
