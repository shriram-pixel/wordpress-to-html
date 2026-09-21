"""Tests for PHP serialization and serialized-data-safe URL replacement.

These matter more than their size suggests: getting them wrong is how a
WordPress conversion silently loses its widgets, menus, theme options and
Elementor layouts.
"""

from __future__ import annotations

import pytest

from app.utils.phpserialize import (
    PhpObject,
    PhpSerializationError,
    dumps,
    is_serialized,
    loads,
    looks_serialized,
    replace_in_serialized,
)


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"N;", None),
        (b"b:1;", True),
        (b"b:0;", False),
        (b"i:42;", 42),
        (b"i:-7;", -7),
        (b'd:1.5;', 1.5),
        (b's:5:"hello";', b"hello"),
        (b's:0:"";', b""),
        (b"a:0:{}", {}),
        (b'a:2:{i:0;s:1:"a";i:1;s:1:"b";}', {0: b"a", 1: b"b"}),
    ],
)
def test_loads(raw: bytes, expected):
    assert loads(raw) == expected


def test_nested_structures_round_trip():
    value = {
        "url": "https://example.com",
        "nested": {"list": [1, 2, 3], "flag": True, "nothing": None},
        "count": 7,
    }
    assert loads(dumps(value)) == {
        "url": b"https://example.com",
        "nested": {"list": {0: 1, 1: 2, 2: 3}, "flag": True, "nothing": None},
        "count": 7,
    }


def test_objects_keep_their_class():
    original = PhpObject("WP_Widget", {"title": "Hello"})
    restored = loads(dumps(original))
    assert isinstance(restored, PhpObject)
    assert restored.class_name == "WP_Widget"
    assert restored.properties["title"] == b"Hello"


def test_string_lengths_are_in_bytes_not_characters():
    """A character-counting implementation corrupts every non-ASCII site."""
    encoded = dumps({"t": "café ✓"})
    assert b's:9:"caf\xc3\xa9 \xe2\x9c\x93";' in encoded
    assert loads(encoded)["t"].decode("utf-8") == "café ✓"


@pytest.mark.parametrize(
    "raw",
    [b's:5:"ab";', b"a:2:{i:0;s:1:\"a\";}", b"i:notanumber;", b"x:1;", b"", b's:99:"short";'],
)
def test_malformed_input_raises(raw: bytes):
    with pytest.raises(PhpSerializationError):
        loads(raw)


def test_trailing_data_is_rejected_in_strict_mode():
    with pytest.raises(PhpSerializationError):
        loads(b"i:1;garbage")
    assert loads(b"i:1;garbage", strict=False) == 1


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def test_is_serialized_distinguishes_real_data():
    assert is_serialized(dumps({"a": 1}))
    assert is_serialized(b"N;")
    assert not is_serialized(b"just a string")
    assert not is_serialized(b"https://example.com")
    assert not is_serialized(b"")


def test_looks_serialized_is_shape_only():
    corrupt = b'a:1:{s:99:"short";}'
    assert looks_serialized(corrupt) is True
    assert is_serialized(corrupt) is False


# ---------------------------------------------------------------------------
# Replacement -- the part that protects a WordPress database
# ---------------------------------------------------------------------------
def test_replacement_recomputes_string_lengths():
    original = dumps({"siteurl": "https://example.com", "n": 3})
    replaced, count = replace_in_serialized(original, {"https://example.com": "http://127.0.0.1:8080"})

    assert count == 1
    assert is_serialized(replaced), "the replacement produced unparseable data"
    assert loads(replaced)["siteurl"] == b"http://127.0.0.1:8080"


def test_replacement_shortening_a_string_is_also_safe():
    original = dumps({"u": "https://a-very-long-domain-name.example.com/path"})
    replaced, _ = replace_in_serialized(original, {"https://a-very-long-domain-name.example.com": "http://x"})
    assert is_serialized(replaced)
    assert loads(replaced)["u"] == b"http://x/path"


def test_replacement_reaches_deeply_nested_values():
    original = dumps({
        "a": {"b": [PhpObject("stdClass", {"url": "https://example.com/x"})]},
        "unrelated": 5,
    })
    replaced, count = replace_in_serialized(original, {"https://example.com": "http://local"})

    assert count == 1
    assert is_serialized(replaced)
    restored = loads(replaced)
    assert restored["a"]["b"][0].properties["url"] == b"http://local/x"
    assert restored["unrelated"] == 5


def test_replacement_handles_unicode_payloads():
    original = dumps({"t": "café https://example.com ✓"})
    replaced, _ = replace_in_serialized(original, {"https://example.com": "http://127.0.0.1:1"})
    assert is_serialized(replaced)
    assert loads(replaced)["t"].decode("utf-8") == "café http://127.0.0.1:1 ✓"


def test_plain_strings_are_replaced_directly():
    replaced, count = replace_in_serialized("visit https://example.com now",
                                            {"https://example.com": "http://x"})
    assert replaced == "visit http://x now"
    assert count == 1


def test_data_that_only_looks_serialized_is_left_untouched():
    """Byte-replacing a malformed serialized value would deepen the damage."""
    corrupt = 'a:1:{s:99:"https://example.com";}'
    replaced, count = replace_in_serialized(corrupt, {"https://example.com": "http://x"})
    assert replaced == corrupt
    assert count == 0


def test_no_match_returns_the_original_object_unchanged():
    original = dumps({"a": "nothing here"})
    replaced, count = replace_in_serialized(original, {"https://example.com": "http://x"})
    assert count == 0
    assert replaced == original


def test_return_type_matches_input_type():
    as_str, _ = replace_in_serialized("https://example.com", {"https://example.com": "http://x"})
    as_bytes, _ = replace_in_serialized(b"https://example.com", {"https://example.com": "http://x"})
    assert isinstance(as_str, str)
    assert isinstance(as_bytes, bytes)


def test_keys_are_replaced_as_well_as_values():
    original = dumps({"https://example.com": "value"})
    replaced, count = replace_in_serialized(original, {"https://example.com": "http://local"})
    assert count == 1
    assert "http://local" in loads(replaced)


def test_multiple_replacements_apply_together():
    original = dumps({"a": "https://example.com", "b": "//example.com/x"})
    replaced, count = replace_in_serialized(original, {
        "https://example.com": "http://127.0.0.1:1",
        "//example.com": "//127.0.0.1:1",
    })
    assert count >= 2
    assert is_serialized(replaced)
