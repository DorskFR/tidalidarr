import asyncio
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from random import randrange

import pykakasi

kks = pykakasi.kakasi()
logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PATTERN = re.compile(r'[\/\\:\*\?"<>\|]')
NON_WORD = re.compile(r"[\W_]+")


def sanitize(text: str) -> str:
    return PATTERN.sub("", text)


def romanize(text: str) -> str:
    return "".join(p["hepburn"] for p in kks.convert(text))


def contains_japanese(text: str) -> bool:
    unicode_ranges = [("\u3040", "\u309f"), ("\u30a0", "\u30ff"), ("\u4e00", "\u9faf")]
    return any(start <= char <= end for char in text for start, end in unicode_ranges)


async def jitter_sleep(sleep_time: float) -> None:
    random_time = randrange(int(sleep_time * 1000 * 0.8), int(sleep_time * 1000 * 1.2)) / 1000
    logger.info(f"😴 Sleeping {random_time:.2f} seconds")
    await asyncio.sleep(random_time)


def normalize(text: str) -> str:
    return NON_WORD.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()
