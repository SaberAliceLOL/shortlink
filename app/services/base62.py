"""Base62 编解码：ID ↔ 短码。

字符表顺序即进位权重（这里是 0-9A-Za-z）。
ID 起点 62^4，所以生产路径上短码恒为 5-7 位（62^7 ≈ 3.5 万亿）。

Week 3 加了 `is_valid_code`：以前用 `code.isalnum()` 做廉价防御，但 Python 的
`isalnum()` 对 Unicode 也返回 True（比如 "１２３"、"café"），这些字符进不了
Base62 字符表却照样能落到 SQL 里跑一趟。用显式字符集判断更准，
也顺手让水位守卫能安全地 decode。
"""

BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(BASE62_ALPHABET)
BASE62_CHARS = frozenset(BASE62_ALPHABET)

# 短码长度区间：CHAR(7) 的列宽是上界，下界来自 ID 起点 62^4
MIN_CODE_LEN = 5
MAX_CODE_LEN = 7


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


def is_valid_code(code: str) -> bool:
    """格式合法 = 长度 5-7 且每个字符都在 Base62 表里。"""
    if not (MIN_CODE_LEN <= len(code) <= MAX_CODE_LEN):
        return False
    return all(ch in BASE62_CHARS for ch in code)
