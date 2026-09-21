"""Compare screenshots of the original and exported pages.

A similarity score is a *diagnostic*, not a verdict. Two pages can score 99%
and still differ in the one place that matters (a missing logo in the header),
and can score 85% purely because a carousel stopped on a different slide. So
this module produces three things and lets a human weigh them:

* a **score** per page and viewport
* a **diff image** with the changed regions boxed, so the eye goes straight to
  what moved
* the **largest differing regions**, described in words, so the report can say
  *where* a page differs rather than only by how much

Scoring uses a perceptual-ish greyscale comparison with a small tolerance,
which ignores antialiasing noise while still catching a missing image or a
collapsed layout.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw

logger = logging.getLogger(__name__)

#: Per-channel difference below this is treated as identical: font rasterisation
#: and JPEG-ish artefacts routinely differ by a few levels between renders.
_NOISE_THRESHOLD = 12

#: Ignore differing regions smaller than this many pixels.
_MIN_REGION_PIXELS = 400


@dataclass(slots=True)
class ViewportComparison:
    viewport: str
    """``desktop`` or ``mobile``."""
    score: float = 0.0
    """0..1, the fraction of pixels that match within tolerance."""
    differing_pixels: int = 0
    total_pixels: int = 0
    height_original: int = 0
    height_static: int = 0
    diff_image: Path | None = None
    regions: list[tuple[int, int, int, int]] = field(default_factory=list)
    note: str = ""

    @property
    def percent(self) -> float:
        return round(self.score * 100, 2)


@dataclass(slots=True)
class PageComparison:
    url: str
    output_path: str
    desktop: ViewportComparison | None = None
    mobile: ViewportComparison | None = None

    @property
    def worst_score(self) -> float:
        scores = [c.score for c in (self.desktop, self.mobile) if c is not None]
        return min(scores) if scores else 0.0


#: Height of the bands a page is compared in, and how far each band may move
#: to find its match. A banner a few pixels taller in the export shifts
#: everything below it; comparing row-for-row would call the whole page
#: different when nothing below the banner changed at all.
_BAND = 160
_MAX_DRIFT = 200
_SEARCH_STEP = 4


def compare_images(
    original_path: Path,
    static_path: Path,
    diff_path: Path,
    viewport: str = "desktop",
) -> ViewportComparison:
    """Compare two screenshots, tolerating vertical shifts, and write a diff image.

    The original is cut into horizontal bands. Each band is matched against the
    export near where the previous band matched, so a shift propagates down the
    page the way it does in a real layout. The score is the share of the
    original's pixels whose matched export pixel is the same within tolerance.
    """
    result = ViewportComparison(viewport=viewport)

    try:
        with Image.open(original_path) as original_image, Image.open(static_path) as static_image:
            original = original_image.convert("RGB")
            static = static_image.convert("RGB")

            result.height_original = original.height
            result.height_static = static.height

            width = min(original.width, static.width)
            if width == 0 or original.height == 0 or static.height == 0:
                result.note = "one of the screenshots was empty"
                return result
            if original.width != width:
                original = original.crop((0, 0, width, original.height))
            if static.width != width:
                static = static.crop((0, 0, width, static.height))

            a = np.asarray(original, dtype=np.int16)
            b = np.asarray(static, dtype=np.int16)
            gray_a = np.asarray(original.convert("L"), dtype=np.int16)
            gray_b = np.asarray(static.convert("L"), dtype=np.int16)
            small_a = np.asarray(original.convert("L").resize(
                (max(1, width // _SEARCH_STEP), max(1, original.height // _SEARCH_STEP))), dtype=np.int16)
            small_b = np.asarray(static.convert("L").resize(
                (max(1, width // _SEARCH_STEP), max(1, static.height // _SEARCH_STEP))), dtype=np.int16)

            mask = np.zeros(a.shape[:2], dtype=bool)
            aligned = np.full_like(a, 255)
            largest_shift = 0

            # Columns move independently -- a sidebar stays put while the main
            # column grows -- so wide pages are also split into column tiles,
            # each following its own drift down the page.
            tiles = _column_tiles(width)
            drifts = [0] * len(tiles)

            for top in range(0, a.shape[0], _BAND):
                bottom = min(top + _BAND, a.shape[0])
                for index, (left, right) in enumerate(tiles):
                    coarse = _best_offset(small_a, small_b, top, bottom, drifts[index],
                                          left // _SEARCH_STEP, max(left // _SEARCH_STEP + 1, right // _SEARCH_STEP))
                    offset = _refine_offset(gray_a, gray_b, top, bottom, left, right, coarse)
                    drifts[index] = offset
                    largest_shift = max(largest_shift, abs(offset))

                    src_top, src_bottom = top + offset, bottom + offset
                    band = a[top:bottom, left:right]
                    if src_top < 0 or src_bottom > b.shape[0]:
                        # The export ends before this part of the original: it
                        # is genuinely missing, so every pixel counts.
                        mask[top:bottom, left:right] = True
                        available_top, available_bottom = max(0, src_top), min(b.shape[0], src_bottom)
                        if available_bottom > available_top:
                            rows = slice(available_top - src_top, available_bottom - src_top)
                            aligned[top:bottom, left:right][rows] = b[available_top:available_bottom, left:right]
                        continue
                    matched = b[src_top:src_bottom, left:right]
                    aligned[top:bottom, left:right] = matched
                    mask[top:bottom, left:right] = np.abs(band - matched).max(axis=2) > _NOISE_THRESHOLD

            result.total_pixels = int(mask.size)
            result.differing_pixels = int(mask.sum())
            result.score = 1.0 - (result.differing_pixels / result.total_pixels)

            notes = []
            if original.height != static.height:
                notes.append(
                    f"page heights differ ({original.height}px original vs {static.height}px export)"
                )
            if largest_shift:
                notes.append(f"content shifted by up to {largest_shift}px; compared after aligning")
            result.note = "; ".join(notes)

            result.regions = _find_regions(mask)
            _write_diff_image(Image.fromarray(aligned.astype(np.uint8)), mask, result.regions, diff_path)
            result.diff_image = diff_path

    except (OSError, ValueError, MemoryError) as exc:
        result.note = f"comparison failed: {exc}"
        logger.warning("visual comparison failed for %s: %s", original_path.name, exc)

    return result


def _column_tiles(width: int) -> list[tuple[int, int]]:
    """Split a page into column tiles: four on desktop widths, one on mobile."""
    count = 4 if width >= 1000 else 1
    edges = [round(width * i / count) for i in range(count + 1)]
    return [(edges[i], edges[i + 1]) for i in range(count)]


def _best_offset(small_a: np.ndarray, small_b: np.ndarray, top: int, bottom: int, drift: int,
                 left: int = 0, right: int | None = None) -> int:
    """Vertical offset (full-resolution px) at which a tile of the original best
    matches the export, searched around the previous offset of the same column."""
    s_top, s_bottom = top // _SEARCH_STEP, max(top // _SEARCH_STEP + 1, bottom // _SEARCH_STEP)
    band = small_a[s_top:s_bottom, left:right]
    if band.size == 0:
        return drift
    centre = drift // _SEARCH_STEP
    # A flat tile (plain background) matches anywhere; keep the current drift.
    if int(band.max()) - int(band.min()) < 8:
        if 0 <= s_top + centre and s_bottom + centre <= small_b.shape[0]:
            return drift
        return 0

    # Most tiles sit exactly where the previous one in their column did.
    lo, hi = s_top + centre, s_bottom + centre
    if lo >= 0 and hi <= small_b.shape[0]:
        if float(np.mean(np.abs(band - small_b[lo:hi, left:right]) > _NOISE_THRESHOLD)) < 0.002:
            return drift

    best, best_cost = drift, None
    reach = _MAX_DRIFT // _SEARCH_STEP
    # Around the previous offset and around zero: a band that differs for real
    # (a missing banner) must not drag every band below it off course.
    candidates = set(range(centre - reach, centre + reach + 1)) | set(range(-reach, reach + 1))
    for candidate in sorted(candidates, key=lambda c: (abs(c - centre), abs(c))):
        lo, hi = s_top + candidate, s_bottom + candidate
        if lo < 0 or hi > small_b.shape[0]:
            continue
        cost = float(np.mean(np.abs(band - small_b[lo:hi, left:right]) > _NOISE_THRESHOLD))
        if best_cost is None or cost < best_cost - 1e-9:
            best, best_cost = candidate * _SEARCH_STEP, cost
    return best


def _refine_offset(a: np.ndarray, b: np.ndarray, top: int, bottom: int, left: int, right: int,
                   coarse: int) -> int:
    """Pin the coarse offset to the exact pixel. Text one pixel out of line
    differs everywhere, so the coarse search alone is not enough."""
    band = a[top:bottom, left:right]   # greyscale: alignment only needs luminance
    best, best_cost = coarse, None
    for candidate in sorted(range(coarse - _SEARCH_STEP, coarse + _SEARCH_STEP + 1),
                            key=lambda c: abs(c - coarse)):
        lo, hi = top + candidate, bottom + candidate
        if lo < 0 or hi > b.shape[0]:
            continue
        cost = float(np.mean(np.abs(band - b[lo:hi, left:right]) > _NOISE_THRESHOLD))
        if best_cost is None or cost < best_cost - 1e-9:
            best, best_cost = candidate, cost
            if cost == 0.0:
                break
    return best


def _find_regions(mask: np.ndarray, max_regions: int = 8) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of the largest contiguous bands of difference.

    A full connected-component labelling would need SciPy; row/column banding
    is enough to point a reader at the right part of the page and costs
    nothing.
    """
    if not mask.any():
        return []

    rows = np.where(mask.any(axis=1))[0]
    if rows.size == 0:
        return []

    # Group rows into bands separated by more than 20 clean rows.
    bands: list[tuple[int, int]] = []
    start = previous = int(rows[0])
    for row in rows[1:]:
        row = int(row)
        if row - previous > 20:
            bands.append((start, previous))
            start = row
        previous = row
    bands.append((start, previous))

    boxes: list[tuple[int, int, int, int]] = []
    for top, bottom in bands:
        band = mask[top:bottom + 1]
        columns = np.where(band.any(axis=0))[0]
        if columns.size == 0:
            continue
        left, right = int(columns[0]), int(columns[-1])
        if (bottom - top + 1) * (right - left + 1) < _MIN_REGION_PIXELS:
            continue
        boxes.append((left, top, right, bottom))

    boxes.sort(key=lambda b: (b[3] - b[1]) * (b[2] - b[0]), reverse=True)
    return boxes[:max_regions]


def _write_diff_image(
    base: Image.Image,
    mask: np.ndarray,
    regions: list[tuple[int, int, int, int]],
    diff_path: Path,
) -> None:
    """Write the static render with differing pixels tinted and regions boxed."""
    diff_path.parent.mkdir(parents=True, exist_ok=True)

    canvas = base.convert("RGB")
    overlay = np.asarray(canvas).copy()

    # Tint differing pixels magenta, keeping the underlying page visible so the
    # reader can see what changed rather than only where.
    overlay[mask] = (overlay[mask] * 0.35 + np.array([255, 0, 128]) * 0.65).astype(np.uint8)

    annotated = Image.fromarray(overlay)
    draw = ImageDraw.Draw(annotated)
    for left, top, right, bottom in regions:
        draw.rectangle([left, top, right, bottom], outline=(255, 215, 0), width=3)

    # Fast compression: these are large images looked at once, if at all.
    annotated.save(diff_path, "PNG", compress_level=1)


class VisualValidator:
    """Screenshots the exported pages and compares them with the originals."""

    def __init__(self, screenshot_dir: Path, options) -> None:
        self.screenshot_dir = Path(screenshot_dir)
        self.options = options
        self.comparisons: list[PageComparison] = []

    async def compare_pages(
        self,
        static_base_url: str,
        pages: list[tuple[str, str, str]],
        renderer_options,
        progress=None,
    ) -> list[PageComparison]:
        """Screenshot and compare a set of pages.

        *pages* is ``[(original_url, output_path, screenshot_slug), ...]``.
        """
        from app.services.browser_renderer import BrowserRenderer, _slug_for

        if not pages:
            return []

        results: list[PageComparison | None] = [None] * len(pages)
        done = 0
        # Screenshots are large, so a handful of pages at a time -- enough to
        # keep a core busy comparing while another page is still loading.
        limit = asyncio.Semaphore(max(1, min(3, getattr(renderer_options, "render_concurrency", 3) or 3)))

        async with BrowserRenderer(
            renderer_options, screenshot_dir=self.screenshot_dir,
            base_url=static_base_url, timeout_ms=30_000, retries=0,
        ) as renderer:
            async def one(index: int, original_url: str, output_path: str, slug: str) -> None:
                nonlocal done
                comparison = PageComparison(url=original_url, output_path=output_path)
                static_url = f"{static_base_url}/{output_path.lstrip('/')}"
                async with limit:
                    try:
                        await self._shoot_and_compare(renderer, static_url, slug, comparison)
                    except Exception as exc:
                        logger.warning("visual comparison failed for %s: %s", output_path, exc)
                results[index] = comparison
                done += 1
                if progress:
                    progress(f"Comparing screenshots ({done}/{len(pages)})", done / len(pages))

            await asyncio.gather(
                *(one(i, *page) for i, page in enumerate(pages)), return_exceptions=True
            )

        self.comparisons = [c for c in results if c is not None]
        return self.comparisons

    async def _shoot_and_compare(self, renderer, static_url: str, slug: str,
                                 comparison: PageComparison) -> None:
        """Capture the static page at both viewports and diff against the original."""
        page = await renderer._context.new_page()
        try:
            for viewport_name, size in (
                ("desktop", self.options.desktop_viewport),
                ("mobile", self.options.mobile_viewport),
            ):
                if viewport_name == "mobile" and not self.options.mobile_validation:
                    continue

                original = self.screenshot_dir / f"{slug}.original.{viewport_name}.png"
                if not original.is_file():
                    continue

                width, height = size
                await page.set_viewport_size({"width": width, "height": height})
                await page.goto(static_url, wait_until="load", timeout=30_000)
                await renderer._scroll_through(page)
                await page.wait_for_timeout(400)

                static_shot = self.screenshot_dir / f"{slug}.static.{viewport_name}.png"
                await page.screenshot(path=str(static_shot), full_page=True, animations="disabled")

                diff_path = self.screenshot_dir / f"{slug}.diff.{viewport_name}.png"
                # Comparing is pure number-crunching; off the event loop it
                # overlaps with the next page's screenshots.
                result = await asyncio.to_thread(
                    compare_images, original, static_shot, diff_path, viewport_name
                )

                if viewport_name == "desktop":
                    comparison.desktop = result
                else:
                    comparison.mobile = result
        finally:
            try:
                await page.close()
            except Exception:
                pass


def summarise(comparisons: list[PageComparison]) -> dict:
    """Aggregate comparison results for the conversion report."""
    desktop_scores = [c.desktop.score for c in comparisons if c.desktop and c.desktop.total_pixels]
    mobile_scores = [c.mobile.score for c in comparisons if c.mobile and c.mobile.total_pixels]

    worst = sorted(
        (c for c in comparisons if c.desktop or c.mobile),
        key=lambda c: c.worst_score,
    )[:10]

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values) * 100, 2) if values else 0.0

    return {
        "pages_compared": len(comparisons),
        "desktop_average": mean(desktop_scores),
        "mobile_average": mean(mobile_scores),
        "desktop_min": round(min(desktop_scores) * 100, 2) if desktop_scores else 0.0,
        "mobile_min": round(min(mobile_scores) * 100, 2) if mobile_scores else 0.0,
        "lowest_scoring": [
            {
                "output_path": c.output_path,
                "url": c.url,
                "desktop": c.desktop.percent if c.desktop else None,
                "mobile": c.mobile.percent if c.mobile else None,
                "note": (c.desktop.note if c.desktop else "") or (c.mobile.note if c.mobile else ""),
            }
            for c in worst
        ],
        "caveat": (
            "Similarity scores are a diagnostic, not a pass mark. A high score can "
            "still hide a missing element, and a low score is often just a carousel "
            "or animation resting in a different position. Review the diff images."
        ),
    }
