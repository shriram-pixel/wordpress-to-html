"""Reading a backup's page count without extracting it.

Validated against two real conversions while it was written: aungmetals came
out at 622 against 622 actually rendered, and tinitamfg at 187 against 223
discovered -- low, because a crawl also reaches category and paged archive
URLs that no row in ``posts`` corresponds to. That is the accuracy this is
for, and why the interface calls the number "about".

The fixtures below are built the way the two real archives are, including the
two ways they differ: one quotes every value in its SQL dump and the other
does not, and one carries a plugin's npm ``package.json`` that would blank the
archive's own metadata if the folder in the header were ignored.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.backup_probe import HEADER_SIZE, probe  # noqa: E402


def entry(name: str, body: bytes, folder: str = ".") -> bytes:
    """One .wpress entry: a 4377-byte header and then the bytes."""
    head = bytearray(b"\0" * HEADER_SIZE)
    head[0:len(name)] = name.encode()
    size = str(len(body)).encode()
    head[255:255 + len(size)] = size
    stamp = b"1700000000"
    head[269:269 + len(stamp)] = stamp
    head[281:281 + len(folder)] = folder.encode()
    return bytes(head) + body


def row(post_id: int, status: str, kind: str, *, quoted: bool = False,
        content: str = "hello") -> bytes:
    """One posts INSERT, in either of the two dump styles seen in the wild."""
    q = (lambda v: f"'{v}'") if quoted else (lambda v: str(v))
    return (
        f"INSERT INTO `SERVMASK_PREFIX_posts` VALUES ({q(post_id)},{q(1)},"
        f"'2024-01-01 00:00:00','2024-01-01 00:00:00','{content}','Title {post_id}',"
        f"'','{status}','closed','closed','','slug-{post_id}','','',"
        f"'2024-01-01 00:00:00','2024-01-01 00:00:00','',{q(0)},"
        f"'https://example.com/?p={post_id}',{q(0)},'{kind}','',{q(0)});\n"
    ).encode()


def archive(tmp_path: Path, rows: bytes, *, package: dict | None = None,
            extra: bytes = b"", name: str = "site.wpress") -> Path:
    package = {"SiteURL": "https://example.com", "WordPress": {"Version": "6.8.3"},
               "Stylesheet": "hello-elementor", "Plugins": ["a/a.php", "b/b.php"]} \
        if package is None else package

    blob = entry("package.json", json.dumps(package).encode())
    blob += extra
    blob += entry("database.sql", b"START TRANSACTION;\n" + rows)
    blob += b"\0" * HEADER_SIZE          # the end-of-archive marker

    path = tmp_path / name
    path.write_bytes(blob)
    return path


def test_counts_published_pages_and_posts(tmp_path):
    rows = b"".join([
        row(1, "publish", "page"), row(2, "publish", "page"),
        row(3, "publish", "post"), row(4, "draft", "page"),
        row(5, "publish", "revision"), row(6, "publish", "attachment"),
        row(7, "publish", "nav_menu_item"), row(8, "trash", "post"),
    ])
    facts = probe(archive(tmp_path, rows))

    assert facts.pages == 3, "two published pages and one published post"
    assert facts.by_type["page"] == 2
    assert facts.complete


def test_reads_dumps_that_quote_every_value(tmp_path):
    """aungmetals writes ('3','1',...); tinitamfg writes (3,1,...)."""
    rows = b"".join([row(n, "publish", "page", quoted=True) for n in range(1, 6)])
    facts = probe(archive(tmp_path, rows))

    assert facts.pages == 5


def test_custom_post_types_count_as_pages(tmp_path):
    """A product or a case study is a page on the exported site."""
    rows = row(1, "publish", "product") + row(2, "publish", "case_study")
    facts = probe(archive(tmp_path, rows))

    assert facts.pages == 2


def test_a_plugins_own_package_json_cannot_blank_the_metadata(tmp_path):
    """The real archives carry both; only the one at the root is the site's.

    Reading the wrong one replaced a real site URL, theme and plugin list with
    empty strings -- and said nothing, because an npm manifest is valid JSON.
    """
    npm = entry("package.json",
                json.dumps({"name": "some-plugin", "version": "1.0.0"}).encode(),
                folder="wp-content/plugins/thing")
    facts = probe(archive(tmp_path, row(1, "publish", "page"), extra=npm))

    assert facts.site_url == "https://example.com"
    assert facts.theme == "hello-elementor"
    assert facts.plugins == 2
    assert facts.pages == 1


def test_metadata_is_read_even_when_there_is_no_database(tmp_path):
    path = tmp_path / "meta-only.wpress"
    path.write_bytes(entry("package.json", json.dumps(
        {"SiteURL": "https://example.com", "Stylesheet": "twentytwentyfour"}).encode())
        + b"\0" * HEADER_SIZE)

    facts = probe(path)

    assert facts.site_url == "https://example.com"
    assert facts.pages == 0
    assert not facts.complete
    assert "no database" in facts.note


def test_an_empty_site_says_so_rather_than_guessing(tmp_path):
    facts = probe(archive(tmp_path, row(1, "draft", "page")))

    assert facts.pages == 0
    assert not facts.complete
    assert "no published content" in facts.note


def test_content_full_of_commas_and_quotes_does_not_derail_the_count(tmp_path):
    """Page-builder content holds JSON, URLs and escaped quotes.

    Only the tail of each row is parsed for exactly this reason: the middle
    cannot be split on commas at all.
    """
    nasty = ("{\\\"elType\\\":\\\"container\\\",\\\"settings\\\":{\\\"title\\\":"
             "\\\"a,b,c\\\"}},'publish','not really'")
    rows = row(1, "publish", "page", content=nasty) + row(2, "publish", "page")
    facts = probe(archive(tmp_path, rows))

    assert facts.by_type["page"] == 2


def test_the_archive_is_read_not_extracted(tmp_path):
    """Only the database is read; the file bytes are seeked over.

    This is the whole point: a 3 GB archive must not cost 9 GB of disk and
    five minutes to answer one number on a form.
    """
    bulk = entry("big.bin", b"x" * 400_000, folder="wp-content/uploads")
    facts = probe(archive(tmp_path, row(1, "publish", "page"), extra=bulk))

    assert facts.pages == 1
    assert facts.read_bytes < 100_000, "the 400 KB upload must never be read"
