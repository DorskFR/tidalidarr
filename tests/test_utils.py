import pytest

from tidalidarr.utils import contains_japanese, romanize


# Test cases for the romanize function
@pytest.mark.parametrize(
    ("input_text", "expected_output"),
    [
        ("こんにちは", "konnichiha"),
        ("さようなら", "sayounara"),
        ("日本語", "nihongo"),
        ("テスト", "tesuto"),
        ("アルバイト", "arubaito"),
        ("English", "English"),
        ("123", "123"),
        ("", ""),
    ],
)
def test_romanize(input_text, expected_output):
    assert romanize(input_text) == expected_output, f"Expected {expected_output} for input {input_text}"


# Test cases for the contains_japanese function
@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("こんにちは", True),
        ("日本語", True),
        ("テスト", True),
        ("アルバイト", True),
        ("Hello, World!", False),
        ("123", False),
        ("これはテストです。", True),
        ("This is a test.", False),
        ("12345こんにちは", True),
        ("", False),
    ],
)
def test_contains_japanese(input_text, expected):
    assert contains_japanese(input_text) == expected, f"Expected {expected} for input {input_text}"
