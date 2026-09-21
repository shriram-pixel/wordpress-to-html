"""A PHP ``serialize()`` / ``unserialize()`` implementation.

WordPress stores structured option values, widget configuration, theme mods,
Elementor page data and post meta as PHP-serialized strings. Every one of
those strings encodes the *byte length* of each nested string:

    a:1:{s:9:"site_url";s:19:"https://example.com";}

That length prefix is why a plain textual search-and-replace of the site URL
corrupts a WordPress database: shortening ``https://example.com`` to
``http://127.0.0.1:8080`` changes the string's length, the recorded ``s:19``
no longer matches, and PHP silently fails to unserialize the value. The
practical symptom is a site that loses its widgets, menus, theme settings and
Elementor layouts -- exactly the things this tool exists to preserve.

So replacement is done structurally: parse the value, walk it, replace inside
the leaf strings, and re-serialize with recomputed lengths.

Notes on fidelity:

* Lengths are counted in **bytes**, not characters, which matters for any site
  with non-ASCII content. All parsing therefore happens on ``bytes``.
* Objects (``O:``) are preserved as :class:`PhpObject` so a round-trip through
  this module does not destroy class information.
* Malformed input raises :class:`PhpSerializationError` and callers treat the
  value as opaque rather than guessing, which is the safe failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PhpSerializationError",
    "PhpObject",
    "loads",
    "dumps",
    "is_serialized",
    "replace_in_serialized",
]


class PhpSerializationError(ValueError):
    """The byte string is not valid PHP-serialized data."""


@dataclass(slots=True)
class PhpObject:
    """A serialized PHP object (``O:len:"Class":n:{...}``)."""

    class_name: str
    properties: dict[Any, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class _Parser:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    # -- low-level ----------------------------------------------------------
    def _expect(self, literal: bytes) -> None:
        if not self.data.startswith(literal, self.pos):
            raise PhpSerializationError(
                f"expected {literal!r} at offset {self.pos}, "
                f"found {self.data[self.pos:self.pos + 8]!r}"
            )
        self.pos += len(literal)

    def _read_until(self, terminator: bytes) -> bytes:
        index = self.data.find(terminator, self.pos)
        if index < 0:
            raise PhpSerializationError(
                f"unterminated token at offset {self.pos}: no {terminator!r} found"
            )
        chunk = self.data[self.pos:index]
        self.pos = index + len(terminator)
        return chunk

    # -- values -------------------------------------------------------------
    def parse(self) -> Any:
        if self.pos >= len(self.data):
            raise PhpSerializationError("unexpected end of data")

        marker = self.data[self.pos:self.pos + 1]

        if marker == b"N":
            self._expect(b"N;")
            return None
        if marker == b"b":
            self._expect(b"b:")
            raw = self._read_until(b";")
            if raw not in (b"0", b"1"):
                raise PhpSerializationError(f"invalid boolean payload {raw!r}")
            return raw == b"1"
        if marker == b"i":
            self._expect(b"i:")
            raw = self._read_until(b";")
            try:
                return int(raw)
            except ValueError as exc:
                raise PhpSerializationError(f"invalid integer {raw!r}") from exc
        if marker == b"d":
            self._expect(b"d:")
            raw = self._read_until(b";")
            lowered = raw.lower()
            if lowered == b"nan":
                return float("nan")
            if lowered in (b"inf", b"+inf"):
                return float("inf")
            if lowered == b"-inf":
                return float("-inf")
            try:
                return float(raw)
            except ValueError as exc:
                raise PhpSerializationError(f"invalid float {raw!r}") from exc
        if marker == b"s":
            return self._parse_string()
        if marker == b"a":
            return self._parse_array()
        if marker == b"O":
            return self._parse_object()

        raise PhpSerializationError(f"unknown type marker {marker!r} at offset {self.pos}")

    def _parse_string(self) -> bytes:
        self._expect(b"s:")
        length_raw = self._read_until(b":")
        try:
            length = int(length_raw)
        except ValueError as exc:
            raise PhpSerializationError(f"invalid string length {length_raw!r}") from exc
        if length < 0:
            raise PhpSerializationError(f"negative string length {length}")

        self._expect(b'"')
        end = self.pos + length
        if end > len(self.data):
            raise PhpSerializationError(
                f"string at offset {self.pos} claims {length} bytes but only "
                f"{len(self.data) - self.pos} remain"
            )
        value = self.data[self.pos:end]
        self.pos = end
        self._expect(b'";')
        return value

    def _parse_count(self, marker: bytes) -> int:
        self._expect(marker)
        raw = self._read_until(b":")
        try:
            count = int(raw)
        except ValueError as exc:
            raise PhpSerializationError(f"invalid element count {raw!r}") from exc
        if count < 0:
            raise PhpSerializationError(f"negative element count {count}")
        return count

    def _parse_pairs(self, count: int) -> dict[Any, Any]:
        self._expect(b"{")
        result: dict[Any, Any] = {}
        for _ in range(count):
            key = self.parse()
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="surrogateescape")
            value = self.parse()
            result[key] = value
        self._expect(b"}")
        return result

    def _parse_array(self) -> dict[Any, Any]:
        count = self._parse_count(b"a:")
        return self._parse_pairs(count)

    def _parse_object(self) -> PhpObject:
        self._expect(b"O:")
        name_length = int(self._read_until(b":"))
        self._expect(b'"')
        class_name = self.data[self.pos:self.pos + name_length].decode("utf-8", "surrogateescape")
        self.pos += name_length
        self._expect(b'":')
        count = int(self._read_until(b":"))
        return PhpObject(class_name, self._parse_pairs(count))


def loads(data: bytes | str, *, strict: bool = True) -> Any:
    """Parse PHP-serialized *data*.

    Strings come back as ``bytes`` so byte-exact re-serialization is possible.
    With ``strict`` the whole input must be consumed.
    """
    if isinstance(data, str):
        data = data.encode("utf-8", errors="surrogateescape")
    parser = _Parser(data)
    value = parser.parse()
    if strict and parser.pos != len(data):
        # Trailing whitespace is common and harmless; anything else is not.
        if data[parser.pos:].strip():
            raise PhpSerializationError(
                f"{len(data) - parser.pos} trailing bytes after the serialized value"
            )
    return value


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def dumps(value: Any) -> bytes:
    """Serialize *value* back into PHP's format with correct byte lengths."""
    if value is None:
        return b"N;"
    if isinstance(value, bool):
        return b"b:1;" if value else b"b:0;"
    if isinstance(value, int):
        return b"i:%d;" % value
    if isinstance(value, float):
        # PHP's serialize_precision=17 default round-trips a double exactly.
        return b"d:" + repr(value).encode("ascii") + b";"
    if isinstance(value, bytes):
        return b's:%d:"%s";' % (len(value), value)
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="surrogateescape")
        return b's:%d:"%s";' % (len(encoded), encoded)
    if isinstance(value, PhpObject):
        name = value.class_name.encode("utf-8", errors="surrogateescape")
        body = b"".join(_dump_key(k) + dumps(v) for k, v in value.properties.items())
        return b'O:%d:"%s":%d:{%s}' % (len(name), name, len(value.properties), body)
    if isinstance(value, dict):
        body = b"".join(_dump_key(k) + dumps(v) for k, v in value.items())
        return b"a:%d:{%s}" % (len(value), body)
    if isinstance(value, (list, tuple)):
        body = b"".join(dumps(i) + dumps(v) for i, v in enumerate(value))
        return b"a:%d:{%s}" % (len(value), body)

    raise PhpSerializationError(f"cannot serialize {type(value).__name__}")


def _dump_key(key: Any) -> bytes:
    """Array keys in PHP are only ever integers or strings."""
    if isinstance(key, bool):
        return b"i:%d;" % int(key)
    if isinstance(key, int):
        return b"i:%d;" % key
    return dumps(key if isinstance(key, (bytes, str)) else str(key))


# ---------------------------------------------------------------------------
# Detection and replacement
# ---------------------------------------------------------------------------
def looks_serialized(value: bytes | str) -> bool:
    """Shape-only test: does *value* have the outline of serialized data?

    Deliberately does **not** parse. This distinguishes "ordinary text" from
    "claims to be serialized", which is the distinction that decides whether a
    textual replacement is safe -- see :func:`replace_in_serialized`.
    """
    if isinstance(value, str):
        value = value.encode("utf-8", errors="surrogateescape")
    value = value.strip()
    if len(value) < 4:
        return value == b"N;"
    if value == b"N;":
        return True
    if value[1:2] != b":":
        return False
    if value[0:1] not in (b"s", b"a", b"O", b"b", b"i", b"d"):
        return False
    return value.endswith((b";", b"}"))


def is_serialized(value: bytes | str) -> bool:
    """Whether *value* is valid PHP-serialized data that actually parses.

    Mirrors WordPress's own ``is_serialized()``: a quick shape test first, so
    the expensive parse is only attempted for plausible candidates.
    """
    if not looks_serialized(value):
        return False
    try:
        loads(value.strip() if isinstance(value, bytes) else value.strip())
    except PhpSerializationError:
        return False
    return True


def _replace_bytes(data: bytes, pairs: list[tuple[bytes, bytes]]) -> tuple[bytes, int]:
    count = 0
    for needle, replacement in pairs:
        if needle and needle in data:
            count += data.count(needle)
            data = data.replace(needle, replacement)
    return data, count


def replace_in_serialized(
    value: bytes | str,
    replacements: dict[str, str] | list[tuple[str, str]],
) -> tuple[bytes | str, int]:
    """Apply *replacements* to *value*, safely whether or not it is serialized.

    Returns ``(new_value, number_of_substitutions)``. The return type matches
    the input type so callers can write the result straight back to the column
    it came from.

    When the value is serialized it is parsed, rewritten leaf-by-leaf and
    re-serialized with corrected lengths. When it is a plain string it is
    replaced directly. When it *looks* serialized but will not parse -- which
    happens with values that were already corrupted, or double-serialized -- it
    is left untouched and reported as zero replacements, because a textual
    replacement there would only deepen the damage.
    """
    was_str = isinstance(value, str)
    raw = value.encode("utf-8", errors="surrogateescape") if was_str else value

    pairs = [
        (k.encode("utf-8", "surrogateescape"), v.encode("utf-8", "surrogateescape"))
        for k, v in (replacements.items() if isinstance(replacements, dict) else replacements)
    ]

    # Nothing to do: skip the parse entirely. This is the common case and the
    # check keeps a multi-hundred-megabyte database import quick.
    if not any(needle in raw for needle, _ in pairs):
        return value, 0

    if looks_serialized(raw):
        # The value claims to be serialized, so a textual replacement would
        # desynchronise its length prefixes. Either rewrite it structurally or
        # leave it completely alone -- never fall back to byte replacement.
        try:
            parsed = loads(raw.strip())
        except PhpSerializationError:
            return value, 0
        rewritten, count = _walk_replace(parsed, pairs)
        if count == 0:
            return value, 0
        out = dumps(rewritten)
        return (out.decode("utf-8", errors="surrogateescape") if was_str else out), count

    out, count = _replace_bytes(raw, pairs)
    return (out.decode("utf-8", errors="surrogateescape") if was_str else out), count


def _walk_replace(node: Any, pairs: list[tuple[bytes, bytes]]) -> tuple[Any, int]:
    """Recursively rewrite every leaf string in a parsed structure."""
    if isinstance(node, bytes):
        return _replace_bytes(node, pairs)

    if isinstance(node, str):
        encoded = node.encode("utf-8", "surrogateescape")
        out, count = _replace_bytes(encoded, pairs)
        return out.decode("utf-8", errors="surrogateescape"), count

    if isinstance(node, dict):
        total = 0
        result: dict[Any, Any] = {}
        for key, value in node.items():
            # Keys are replaced too: WordPress stores URLs as option keys in a
            # few places, notably the transient and rewrite-rule caches.
            new_key, key_count = _walk_replace(key, pairs)
            new_value, value_count = _walk_replace(value, pairs)
            if isinstance(new_key, bytes):
                new_key = new_key.decode("utf-8", errors="surrogateescape")
            result[new_key] = new_value
            total += key_count + value_count
        return result, total

    if isinstance(node, PhpObject):
        properties, count = _walk_replace(node.properties, pairs)
        return PhpObject(node.class_name, properties), count

    if isinstance(node, (list, tuple)):
        total = 0
        result_list = []
        for item in node:
            new_item, count = _walk_replace(item, pairs)
            result_list.append(new_item)
            total += count
        return result_list, total

    # int, float, bool, None: nothing to replace.
    return node, 0
