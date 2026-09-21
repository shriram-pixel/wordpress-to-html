"""Every request a page makes is classified: save it, ignore it, or block it."""

from __future__ import annotations

import pytest

from app.services.request_policy import RequestAction, classify_request

SITE = "http://127.0.0.1:8080"


@pytest.mark.parametrize(
    "url,resource_type",
    [
        (SITE + "/wp-content/themes/t/style.css", "stylesheet"),
        (SITE + "/wp-includes/js/jquery.js", "script"),
        (SITE + "/wp-content/uploads/2026/01/photo.jpg", "image"),
        (SITE + "/wp-content/uploads/hero.webp", "image"),
        (SITE + "/wp-content/themes/t/fonts/x.woff2", "font"),
        (SITE + "/wp-content/uploads/clip.mp4", "media"),
        (SITE + "/site.webmanifest", "manifest"),
        ("https://fonts.gstatic.com/s/x/font.woff2", "font"),
        ("https://secure.gravatar.com/avatar/abc", "image"),
    ],
)
def test_files_the_export_needs_are_saved(url: str, resource_type: str):
    assert classify_request(url, resource_type) is RequestAction.SAVE


@pytest.mark.parametrize(
    "url,resource_type",
    [
        ("https://www.googletagmanager.com/gtag/js?id=G-1", "script"),
        ("https://www.google-analytics.com/collect", "image"),
        ("https://connect.facebook.net/en_US/fbevents.js", "script"),
        ("https://static.hotjar.com/c/hotjar-1.js", "script"),
        ("https://bat.bing.com/bat.js", "script"),
        ("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js", "script"),
        ("https://browser.sentry-cdn.com/7/bundle.min.js", "script"),
        ("https://embed.tawk.to/123/default", "script"),
        ("https://api.wordpress.org/core/version-check/1.7/", "fetch"),
        ("https://downloads.wordpress.org/plugin/x.zip", "fetch"),
        (SITE + "/wp-cron.php?doing_wp_cron=1", "fetch"),
        (SITE + "/xmlrpc.php", "fetch"),
        (SITE + "/wp-admin/load-styles.php", "stylesheet"),
        (SITE + "/wp-login.php", "document"),
        (SITE + "/track.gif", "ping"),
    ],
)
def test_tracking_admin_and_update_traffic_is_blocked(url: str, resource_type: str):
    assert classify_request(url, resource_type) is RequestAction.BLOCK


def test_the_wordpress_heartbeat_is_blocked_but_builder_calls_are_not():
    heartbeat = classify_request(
        SITE + "/wp-admin/admin-ajax.php", "xhr", method="POST",
        post_data="action=heartbeat&_nonce=abc",
    )
    assert heartbeat is RequestAction.BLOCK

    # A page builder loads real content through the same address.
    builder = classify_request(
        SITE + "/wp-admin/admin-ajax.php", "xhr", method="POST",
        post_data="action=elementor_ajax&widget=posts",
    )
    assert builder is RequestAction.IGNORE

    # Unreadable body: let it through rather than risk losing content.
    unknown = classify_request(
        SITE + "/wp-admin/admin-ajax.php", "xhr", method="POST", post_data=None
    )
    assert unknown is RequestAction.IGNORE


@pytest.mark.parametrize(
    "url,resource_type",
    [
        (SITE + "/wp-json/wp/v2/posts", "fetch"),
        (SITE + "/about/", "document"),
        (SITE + "/wp-admin/admin-ajax.php?action=get_more", "xhr"),
    ],
)
def test_runtime_requests_are_made_but_not_saved(url: str, resource_type: str):
    assert classify_request(url, resource_type) is RequestAction.IGNORE


def test_video_follows_the_download_media_option():
    url = SITE + "/wp-content/uploads/clip.mp4"
    assert classify_request(url, "media", download_media=True) is RequestAction.SAVE
    assert classify_request(url, "media", download_media=False) is RequestAction.IGNORE


def test_a_lookalike_host_is_not_blocked():
    """notgoogle-analytics.example is not google-analytics.com."""
    assert classify_request(
        "https://notgoogle-analytics.example/app.js", "script"
    ) is RequestAction.SAVE
