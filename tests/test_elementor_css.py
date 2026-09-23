"""Elementor must not keep a page's styling in a file the backup may lack.

By default it writes each page's CSS to
wp-content/uploads/elementor/css/post-N.css and generates it on demand. A
backup taken after that cache was cleared has none of them; the ones Elementor
never regenerates are requested by the browser and answered with WordPress's
"not found" page, which the asset collector used to save as the stylesheet.

On one real conversion that left 48 such files and cost the home page its hero
background -- 91% against the original, with nothing in the report naming the
cause. Two defences, tested here: print the CSS into the page instead, and
refuse an HTML response for a stylesheet.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.asset_manager import _is_html_response  # noqa: E402
from app.services.wordpress_restorer import configure_for_static_export  # noqa: E402


class FakeCursor:
    def __init__(self, store, deleted=None):
        self.store = store
        self.deleted = deleted if deleted is not None else []
        self.rowcount = 0
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        if sql.upper().startswith("DELETE"):
            self.deleted.append((sql, params))
            self.rowcount = 7 if "postmeta" in sql else 1
            for value in params:
                self.store.pop(value, None)
        elif "INSERT" in sql.upper() or "REPLACE" in sql.upper() or "UPDATE" in sql.upper():
            if len(params) >= 2:
                self.store[params[0]] = params[1]
        self.last = None
    def fetchone(self): return None


class FakeConn:
    def __init__(self, store, deleted): self.store, self.deleted = store, deleted
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return FakeCursor(self.store, self.deleted)
    def commit(self): pass


class FakeServer:
    def __init__(self):
        self.options = {}
        self.deleted: list = []
    def connect(self, database=None): return FakeConn(self.options, self.deleted)


def test_elementor_is_told_to_print_css_into_the_page():
    server = FakeServer()

    notes = configure_for_static_export(server, "db", "wp_", "http://127.0.0.1:8000")

    assert server.options.get("elementor_css_print_method") == "internal"
    assert any("Elementor" in n for n in notes), "the report should say so"


def test_the_usual_options_are_still_set():
    """The new setting must not have displaced anything."""
    server = FakeServer()

    configure_for_static_export(server, "db", "wp_", "http://127.0.0.1:8000")

    assert server.options["siteurl"] == "http://127.0.0.1:8000"
    assert server.options["home"] == "http://127.0.0.1:8000"
    assert server.options["blog_public"] == "1"


def test_a_stylesheet_answered_with_a_page_is_refused():
    """The second defence, for sites that are not Elementor at all."""
    url = "http://127.0.0.1:8000/wp-content/uploads/elementor/css/post-26472.css"

    assert _is_html_response(url, "text/html; charset=UTF-8")
    assert not _is_html_response(url, "text/css")


def test_elementors_record_of_generated_css_is_cleared():
    """The half that was missing: the setting alone changed nothing.

    Each post keeps an ``_elementor_css`` meta saying its stylesheet exists
    and is current. While that is there Elementor enqueues the file instead of
    printing anything -- and the file was never in the backup, because
    uploads/elementor/css is a cache. The database claimed a file that did not
    exist, so nothing regenerated it and nothing reported it missing.
    """
    server = FakeServer()

    notes = configure_for_static_export(server, "db", "wp_", "http://127.0.0.1:8000")

    statements = " | ".join(sql for sql, _ in server.deleted)
    params = [p for _, ps in server.deleted for p in ps]

    assert "wp_postmeta" in statements
    assert "_elementor_css" in params, "the per-post record"
    assert "_elementor_global_css" in params, "and the global one"
    assert any("stylesheet(s)" in n for n in notes), "the report should say how many"


def test_a_site_without_elementor_is_not_disturbed():
    """No such rows exist; the restore must not care."""
    server = FakeServer()

    notes = configure_for_static_export(server, "db", "wp_", "http://127.0.0.1:8000")

    assert server.options["siteurl"] == "http://127.0.0.1:8000"
    assert notes, "it still reports what it did"
