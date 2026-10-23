from counts import word_count
from report import summary
from text import slugify


def test_slugify():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  Multiple   spaces here ") == "multiple-spaces-here"


def test_word_count():
    assert word_count("one two  three") == 3
    assert word_count("") == 0


def test_summary():
    assert summary("Hello big World") == "hello-big-world (3 words)"
