"""What to do with every request a page makes: save it, ignore it, or block it.

Rendering a page fires hundreds of requests, and they are not equal. Some are
the site -- its stylesheets, scripts, images and fonts -- and belong in the
export. Some are needed to build the page but have no place in it: a REST call
whose answer is already baked into the captured DOM. The rest exist only to
watch visitors, and fetching them costs time twice over: once for the request,
and again because analytics and chat widgets keep firing, so the page never
goes quiet and every capture burns its whole settle budget.

One classification, used by both the renderer (which blocks) and the asset
collector (which saves), so the two can never disagree about a request.

    SAVE    the file is part of the finished site      -> fetch it, keep it
    IGNORE  needed while rendering, not part of it     -> fetch it, drop it
    BLOCK   pure telemetry, admin or update traffic    -> never fetch it
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit


class RequestAction(StrEnum):
    SAVE = "save"
    IGNORE = "ignore"
    BLOCK = "block"


#: Hosts that exist to observe visitors, sell advertising, or run a chat
#: widget. Blocking them during capture is the single largest speed win on a
#: real site: they are never part of the export either way, because external
#: resources keep their original URLs and are not downloaded.
#:
#: Fonts, stylesheets and image CDNs are deliberately absent: they affect
#: layout, and therefore the screenshot.
TRACKING_HOSTS = frozenset({
    # analytics
    "google-analytics.com", "www.google-analytics.com", "ssl.google-analytics.com",
    "googletagmanager.com", "www.googletagmanager.com", "analytics.google.com",
    "clarity.ms", "www.clarity.ms",
    "segment.com", "cdn.segment.com", "api.segment.io",
    "mixpanel.com", "cdn.mxpnl.com", "api.mixpanel.com",
    "matomo.cloud", "cdn.matomo.cloud",
    "quantserve.com", "scorecardresearch.com",
    "statcounter.com", "www.statcounter.com",
    "plausible.io", "cdn.usefathom.com",
    "hotjar.com", "static.hotjar.com", "script.hotjar.com", "in.hotjar.com",
    "mouseflow.com", "luckyorange.com", "cdn.luckyorange.com",
    # advertising
    "googletagservices.com", "googlesyndication.com", "pagead2.googlesyndication.com",
    "doubleclick.net", "stats.g.doubleclick.net",
    "adservice.google.com", "adservice.google.co.in",
    "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "bing.com", "bat.bing.com", "snap.licdn.com", "px.ads.linkedin.com",
    "analytics.tiktok.com", "connect.facebook.net", "facebook.com",
    "www.facebook.com", "facebook.net", "tiktok.com", "linkedin.com",
    # telemetry and error reporting
    "newrelic.com", "js-agent.newrelic.com", "bam.nr-data.net",
    "sentry.io", "browser.sentry-cdn.com",
    "cloudflareinsights.com", "static.cloudflareinsights.com",
    # live chat and marketing widgets
    "intercom.io", "widget.intercom.io", "js.intercomcdn.com",
    "crisp.chat", "client.crisp.chat",
    "tawk.to", "embed.tawk.to",
    "drift.com", "js.driftt.com",
    "hubspot.com", "js.hs-scripts.com", "js.hsadspixel.net", "track.hubspot.com",
    "zdassets.com", "static.zdassets.com",
    "addthis.com", "sharethis.com", "addtoany.com",
})

#: WordPress.org services a live site pings for updates and news. A temporary
#: copy has nothing to update, and these calls are slow when the machine has no
#: internet: each one waits for a DNS timeout on every page.
UPDATE_HOSTS = frozenset({
    "api.wordpress.org", "downloads.wordpress.org", "wordpress.org",
    "planet.wordpress.org", "api.w.org", "profiles.wordpress.org",
})
#: Gravatar is deliberately absent: those are the avatars beside comments and
#: author boxes. They are visible content, so blocking them would change how
#: the page looks.

#: Chromium request kinds that cannot change how a page looks.
_TELEMETRY_TYPES = frozenset({"ping", "beacon", "csp_report"})

#: Fetched to build the page, never part of the finished file. The captured DOM
#: already contains whatever these produced.
_RUNTIME_ONLY_TYPES = frozenset({
    "document", "xhr", "fetch", "websocket", "eventsource", "preflight",
})

#: Requests to the site's own back end. ``admin-ajax.php`` is deliberately not
#: here: page builders use it to load real content while a page renders.
_ADMIN_PATHS = ("/wp-cron.php", "/xmlrpc.php", "/wp-login.php", "/wp-signup.php")

_MEDIA_TYPES = frozenset({"media"})


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _matches(host: str, hosts: frozenset[str]) -> bool:
    if not host:
        return False
    return host in hosts or any(host.endswith("." + blocked) for blocked in hosts)


def is_tracking_host(url: str) -> bool:
    """Whether *url* points at an analytics, advertising or telemetry service."""
    return _matches(_host_of(url), TRACKING_HOSTS)


def classify_request(
    url: str,
    resource_type: str = "",
    *,
    method: str = "GET",
    post_data: str | None = None,
    download_media: bool = True,
) -> RequestAction:
    """Decide what to do with one request a page made."""
    resource_type = (resource_type or "").lower()
    path = (urlsplit(url).path or "").lower()
    host = _host_of(url)

    # ---- BLOCK: nothing here can change what the page looks like ----------
    if resource_type in _TELEMETRY_TYPES:
        return RequestAction.BLOCK
    if _matches(host, TRACKING_HOSTS) or _matches(host, UPDATE_HOSTS):
        return RequestAction.BLOCK
    if any(path.endswith(admin) for admin in _ADMIN_PATHS):
        return RequestAction.BLOCK
    if "/wp-admin/" in path and not path.endswith("/admin-ajax.php"):
        return RequestAction.BLOCK
    if path.endswith("/admin-ajax.php") and _is_heartbeat(method, post_data, url):
        # WordPress polls this every 15-60 seconds to keep an editing session
        # alive. Nothing on a rendered page depends on the answer, and each
        # poll restarts the wait for the page to go quiet.
        return RequestAction.BLOCK

    # ---- IGNORE: needed while rendering, not part of the export -----------
    if resource_type in _RUNTIME_ONLY_TYPES:
        return RequestAction.IGNORE
    if resource_type in _MEDIA_TYPES and not download_media:
        return RequestAction.IGNORE

    # ---- SAVE: stylesheets, scripts, images, fonts, media, manifests ------
    return RequestAction.SAVE


def _is_heartbeat(method: str, post_data: str | None, url: str) -> bool:
    """Only with evidence. Page builders load real content through the same
    address, so a request that cannot be read is allowed through: a slower
    capture is better than a page missing a section."""
    body = (post_data or "").lower()
    return "action=heartbeat" in body or "action=heartbeat" in url.lower()
