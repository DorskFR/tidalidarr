import pykakasi

kks = pykakasi.kakasi()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def romanize(text: str) -> str:
    return "".join(p["hepburn"] for p in kks.convert(text))


def contains_japanese(text: str) -> bool:
    unicode_ranges = [("\u3040", "\u309f"), ("\u30a0", "\u30ff"), ("\u4e00", "\u9faf")]
    return any(start <= char <= end for char in text for start, end in unicode_ranges)
