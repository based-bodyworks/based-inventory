"""Playwright-driven ATC state detection for Based's theme.

Collects visible ATC observations tagged with the product handle of the
card that contains them. Supports variant-aware PDP auditing: when the
caller supplies the list of Shopify variant titles for a PDP, the crawler
clicks each variant in the picker, waits for the ATC to update, and
emits one observation per variant. Without variant_labels, only the
default (as-rendered) state is captured — the right behavior for
collection cards and landing pages.

Why variant-aware: Based runs heavy scent-combo sets (9+ variants per
set: Body Wash scent by Deodorant scent, etc). Many combos are OOS on
purpose. A set PDP rendered in its default state often lands on an
OOS combo, which would false-positive a SALES LEAK flag under the
prior "default variant only" crawl. Iterating the picker lets the
audit match each Shopify variant to its on-site ATC state.

Bot-detection mitigations: real desktop User-Agent, throttled
concurrency, random jitter between page loads.
"""

from __future__ import annotations

import contextlib
import logging
import random
import time
from dataclasses import dataclass
from types import TracebackType

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Route, sync_playwright

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Resource types we don't need for ATC text scanning. Blocking these at the
# context level cuts per-page allocations dramatically (Klaviyo/Postscript/
# Triple Whale/Instant.so all pull heavy assets we don't read). Stylesheets
# are NOT blocked — visibility checks via getBoundingClientRect depend on
# layout, and SOLD OUT vs ADD TO CART differentiation reads display state.
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})

# Memory-saver flags for headless Chromium on a 512Mi container. These are
# the standard "running Chromium in Docker / Render" set: --disable-dev-shm-usage
# avoids /dev/shm exhaustion (Render mounts a small tmpfs), --disable-gpu is
# a no-op for headless but suppresses GPU-init warnings, --no-sandbox is
# required because Render's container runs unprivileged. --single-process
# is intentionally NOT included — it's known to crash on heavy pages.
_LAUNCH_ARGS = ["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"]

# Scan JS: walks the DOM and returns one entry per visible ATC leaf tagged
# with the product handle of its containing card. The scanner runs after
# any variant-picker click so the state reflects the currently-selected
# variant. Dedup is by (product_handle, top_y) since a theme may render
# redundant ATCs (e.g. sticky + inline).
_ATC_SCAN_JS = r"""
() => {
  // Action phrase + optional $-prefixed price suffix(es). Based's primary
  // PDP CTAs render as `<p>ADD TO CART $28.00</p>` via Instant.so (price
  // baked into the leaf). Some show sale + strikethrough as
  // `<p>ADD TO CART $28.00 $40.00</p>`. Bare `<p>ADD TO CART</p>` is also
  // common.
  //
  // The suffix MUST be empty or look like `[$price]` (optionally repeated)
  // so we don't over-match marketing copy like "BUY NOW AND SAVE 30%" or
  // accidentally match "ADD TO CARTRIDGE INSTRUCTIONS". The earlier `\b`
  // version was too permissive (would match BUY NOW + arbitrary trailing
  // text); this version requires either end-of-string or whitespace +
  // dollar-prefixed token(s).
  const ATC_TEXT = /^(ADD TO CART|ADD TO BAG|BUY NOW|SOLD OUT|NOTIFY ME|COMING SOON|PRE[- ]?ORDER)(\s+\$[\d.,]+(\s+\$[\d.,]+)?)?$/i;
  const OOS_TEXT = /^(SOLD OUT|NOTIFY ME|COMING SOON|UNAVAILABLE)(\s+\$[\d.,]+(\s+\$[\d.,]+)?)?$/i;

  function productHandleFromCard(leafEl) {
    let cur = leafEl.parentElement;
    for (let i = 0; i < 20 && cur; i++) {
      const links = cur.querySelectorAll('a[href*="/products/"]');
      const handles = new Set();
      for (const a of links) {
        const href = (a.getAttribute('href') || '').split('?')[0];
        const m = href.match(/\/products\/([^/]+)/);
        if (m) handles.add(m[1]);
      }
      if (handles.size === 1) return [...handles][0];
      cur = cur.parentElement;
    }
    return null;
  }

  const pageHandle = (() => {
    const m = (location.pathname || '').match(/\/products\/([^/]+)/);
    return m ? m[1] : null;
  })();

  // On PDPs (/products/{handle}), the page's own product owns every ATC
  // by default. Card-boundary walks on PDPs are actively harmful because
  // sidebars / cross-sell blocks / recommendation carousels contain links
  // to OTHER products (Conditioner, Leave-in Conditioner, etc.) and the
  // walk picks the first ancestor whose subtree has exactly one /products/
  // link — which is often one of those cross-sells, not the PDP itself.
  // The result was NO_BUY_BUTTON flags on core PDPs like Shampoo because
  // the ATC got tagged with a cross-sell's handle.
  //
  // On non-PDP pages (collections, landing pages, homepage), the card
  // walk is the right thing because a single page hosts many product
  // cards and we need to tag each ATC with the card it belongs to.
  const out = [];
  document.querySelectorAll('*').forEach(el => {
    if (!(el.childNodes && el.childNodes.length === 1 && el.childNodes[0].nodeType === 3)) return;
    const text = (el.textContent || '').trim();
    if (!ATC_TEXT.test(text)) return;
    const rect = el.getBoundingClientRect();
    const visible = rect.width > 0 && rect.height > 0;
    if (!visible) return;
    const handle = pageHandle || productHandleFromCard(el);
    const isOos = OOS_TEXT.test(text);
    out.push({
      product_handle: handle,
      text,
      visible: true,
      enabled: !isOos,
      top_y: Math.round(rect.top + window.scrollY),
    });
  });

  const byKey = new Map();
  for (const obs of out) {
    const k = `${obs.product_handle}::${obs.top_y}`;
    if (!byKey.has(k)) byKey.set(k, obs);
  }
  return [...byKey.values()];
}
"""

# Finder JS: return true if a visible <p> leaf exists with exact text
# matching the given label. The crawler uses this to decide whether to
# invoke Playwright's native locator click (which synthesizes real mouse
# events and reliably triggers React onClick handlers — JS element.click()
# sometimes no-ops in the Based theme, producing state-stuck flags).
_HAS_VARIANT_JS = r"""
(label) => {
  for (const el of document.querySelectorAll('p')) {
    if (!(el.childNodes && el.childNodes.length === 1 && el.childNodes[0].nodeType === 3)) continue;
    if ((el.textContent || '').trim() !== label) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) return true;
  }
  return false;
}
"""


def _block_heavy_assets(route: Route) -> None:
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


@dataclass(frozen=True)
class VariantObservation:
    url: str
    product_handle: str | None
    variant_label: str | None
    present: bool
    enabled: bool
    text: str


class AtcCrawler:
    def __init__(self, headless: bool = True, throttle_ms: tuple = (500, 1500)) -> None:
        self.headless = headless
        self.throttle_ms = throttle_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        # URLs the crawler skipped because the storefront client-side-redirected
        # away from the requested path (e.g. /products/scalp-scrubber → /pages/
        # not-found). atc_audit reads this set to suppress NO_BUY_BUTTON flags
        # for URLs that didn't actually render — the "no observations" there is
        # because the page is genuinely gone, not because the ATC is missing.
        # Populated by audit_url's redirect branch. Lives on the crawler so it
        # accumulates across all per-URL audit calls in a single run.
        self.skipped_urls: set[str] = set()

    def __enter__(self) -> AtcCrawler:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=_LAUNCH_ARGS,
        )
        # Single shared context for the lifetime of the crawler. Per-URL
        # context churn was leaking ~10MB/URL on Render's 512Mi tier; the
        # Jan 2026 ATC audit OOM'd around URL 52/113 even though every
        # context.close() ran. Pages on a shared context still close cleanly
        # and reclaim their per-page state.
        self._context = self._browser.new_context(user_agent=_USER_AGENT)
        self._context.route("**/*", _block_heavy_assets)
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("AtcCrawler must be used as a context manager")
        return self._context.new_page()

    def _throttle(self) -> None:
        low, high = self.throttle_ms
        time.sleep(random.uniform(low, high) / 1000)

    def _observations_from_scan(
        self,
        raw: list[dict] | None,
        url: str,
        variant_label: str | None,
    ) -> list[VariantObservation]:
        # If the URL is a PDP and the ATC's containing card resolved to no
        # product link (cardHandle=null), attribute the observation to the
        # page's own handle rather than leaving it None. Otherwise downstream
        # code treats "no observation matched the page handle" as a hard
        # NO_BUY_BUTTON flag, which is wrong when there IS an ATC visible
        # but the surrounding markup doesn't expose a /products/ link
        # (common on bundle/set PDPs where the SOLD OUT button's container
        # has no related-product anchor for cardHandle to grab).
        url_handle: str | None = None
        if "/products/" in url:
            tail = url.split("/products/", 1)[1]
            url_handle = tail.split("/")[0].split("?")[0] or None

        return [
            VariantObservation(
                url=url,
                product_handle=entry.get("product_handle") or url_handle,
                variant_label=variant_label,
                present=bool(entry.get("visible")),
                enabled=bool(entry.get("enabled")),
                text=entry.get("text", ""),
            )
            for entry in (raw or [])
        ]

    def audit_url(
        self,
        url: str,
        variant_labels: list[str] | None = None,
    ) -> list[VariantObservation]:
        """Audit a URL and return one VariantObservation per visible ATC.

        If `variant_labels` is supplied (PDP mode), the crawler clicks each
        variant in the picker and emits one observation per variant tagged
        with its label. The pre-click/default state is not emitted in this
        mode — it's redundant with whichever variant happens to be active
        by default, and tagging it as "None" would confuse the audit
        matcher. Without variant_labels (collection / landing mode), only
        the as-rendered observations are returned.
        """
        t0 = time.monotonic()
        page = self._new_page()
        needs_lazy_scroll = "/collections/" in url
        try:
            # 20s page-load timeout. Slow Instant.so PDPs (notably the
            # duo / kit pages — shower-duo-cck, definition-hold-duo etc.)
            # consistently breach 10s under prod conditions and then bail
            # before the ATC ever mounts, producing false NO_BUY_BUTTON
            # flags. Avi confirmed live that those pages do have working
            # buy buttons; the bot just wasn't patient enough.
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)

            final_url = page.url.split("#")[0].split("?")[0]
            requested_url = url.split("#")[0].split("?")[0]
            if final_url != requested_url:
                elapsed = time.monotonic() - t0
                logger.info(
                    "Skipped %s (client-side redirect to %s) in %.1fs",
                    url,
                    final_url,
                    elapsed,
                )
                self.skipped_urls.add(url)
                page.close()
                return []

            # Wait for React to hydrate the PAGE'S OWN ATC, not just any
            # ATC-shaped text. Previously we waited for any visible leaf
            # matching the ATC lexicon, which returned early on pages that
            # render a promo banner like "Buy Now" or a cross-sell ATC
            # before the main product's ATC mounted. The scan then ran
            # against a stale DOM and missed the product's own ATC,
            # triggering false NO_BUY_BUTTON flags on live PDPs.
            #
            # For PDPs we require an ATC whose containing-card walk
            # resolves to the URL's own handle (or none, i.e. nothing else
            # claimed it). For non-PDPs (collections, landing pages), any
            # ATC suffices.
            page_handle = None
            if "/products/" in url:
                tail = url.split("/products/", 1)[1]
                page_handle = tail.split("/")[0].split("?")[0] or None
            with contextlib.suppress(Exception):
                page.wait_for_function(
                    """(pageHandle) => {
                        // Mirrors _ATC_SCAN_JS regex: action phrase plus
                        // optional `\\s+\\$price` suffix(es); rejects
                        // marketing banners and 'ADD TO CARTRIDGE'-style
                        // word continuations.
                        const re = /^(ADD TO CART|ADD TO BAG|BUY NOW|SOLD OUT|NOTIFY ME|COMING SOON|PRE[- ]?ORDER)(\s+\$[\d.,]+(\s+\$[\d.,]+)?)?$/i;
                        function cardHandle(leaf) {
                          let cur = leaf.parentElement;
                          for (let i = 0; i < 20 && cur; i++) {
                            const links = cur.querySelectorAll('a[href*="/products/"]');
                            const handles = new Set();
                            for (const a of links) {
                              const h = (a.getAttribute('href') || '').split('?')[0];
                              const m = h.match(/\/products\/([^/]+)/);
                              if (m) handles.add(m[1]);
                            }
                            if (handles.size === 1) return [...handles][0];
                            cur = cur.parentElement;
                          }
                          return null;
                        }
                        for (const el of document.querySelectorAll('*')) {
                          if (!(el.childNodes && el.childNodes.length === 1 &&
                                el.childNodes[0].nodeType === 3)) continue;
                          const t = (el.textContent || '').trim();
                          if (!re.test(t)) continue;
                          const r = el.getBoundingClientRect();
                          if (!(r.width > 0 && r.height > 0)) continue;
                          if (!pageHandle) return true;
                          // For PDPs, only count ATCs that are clearly for
                          // the page's own product (pageHandle match or no
                          // card-level competitor claiming it).
                          const owner = cardHandle(el);
                          if (owner === null || owner === pageHandle) return true;
                        }
                        return false;
                    }""",
                    arg=page_handle,
                    # Paired with the 20s page-load bump above. Originally
                    # 12s; bumped to 25s on 2026-05-21 after the first
                    # post-OOM atc run (2GB plan) produced ~14 false
                    # NO_BUY_BUTTON flags on bundle / set / duo PDPs where
                    # the SPAN-wrapped Add-to-Cart leaf is laid out ~30K
                    # pixels down the page and takes longer to mount under
                    # sequential-batch load (shared browser context across
                    # 100+ URLs). Singles still pass in 1-2s; the bump
                    # only costs time on the genuinely slow-to-hydrate
                    # bundle PDPs.
                    timeout=25_000,
                )

            if needs_lazy_scroll:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(250)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.warning("Failed to load %s after %.1fs: %s", url, elapsed, exc)
            page.close()
            return []

        observations: list[VariantObservation] = []

        try:
            if variant_labels:
                # PDP mode: iterate the variant picker via Playwright's
                # native click (mouse events), not el.click() via evaluate.
                # The JS-level click sometimes no-ops in this theme,
                # leaving the ATC state stuck on a previously-selected
                # variant — which would produce spurious SALES LEAK /
                # OVERSELL RISK flags as each subsequent variant scan
                # reads the stale state.
                matched_labels: list[str] = []
                for label in variant_labels:
                    try:
                        present = page.evaluate(_HAS_VARIANT_JS, label)
                    except Exception as exc:
                        logger.debug("Variant lookup failed for %s / %s: %s", url, label, exc)
                        continue
                    if not present:
                        # Variant label isn't visible on this PDP — could be
                        # archived, hidden behind a nested picker, or have a
                        # label mismatch between Shopify and the theme. Skip
                        # rather than flagging a variant that can't be
                        # observed.
                        continue
                    try:
                        # `get_by_text(label, exact=True).first` finds the
                        # matching <p> leaf. locator.click() scrolls into
                        # view, waits for the element to be actionable, and
                        # fires real MouseEvent sequence (mousemove +
                        # mousedown + mouseup), which triggers React's
                        # synthetic event system reliably.
                        page.get_by_text(label, exact=True).first.click(timeout=2_000)
                    except Exception as exc:
                        logger.debug("Variant click failed for %s / %s: %s", url, label, exc)
                        continue
                    matched_labels.append(label)
                    page.wait_for_timeout(800)
                    raw = page.evaluate(_ATC_SCAN_JS)
                    observations.extend(
                        self._observations_from_scan(raw, url=url, variant_label=label)
                    )
                if not matched_labels:
                    # Picker was missing or none of the labels matched:
                    # fall back to a default-state scan so the audit still
                    # sees the page.
                    raw = page.evaluate(_ATC_SCAN_JS)
                    observations.extend(
                        self._observations_from_scan(raw, url=url, variant_label=None)
                    )
            else:
                # Collection / landing mode: default-state scan only.
                raw = page.evaluate(_ATC_SCAN_JS)
                observations.extend(self._observations_from_scan(raw, url=url, variant_label=None))
        except Exception as exc:
            logger.warning("ATC scan failed on %s: %s", url, exc)
            page.close()
            return []

        elapsed = time.monotonic() - t0
        logger.info(
            "Audited %s in %.1fs (%d obs%s)",
            url,
            elapsed,
            len(observations),
            f", {len(variant_labels)} variants requested" if variant_labels else "",
        )

        self._throttle()
        page.close()
        return observations

    def audit_inline_html(
        self,
        html: str,
        url: str,
        variant_labels: list[str] | None = None,
    ) -> list[VariantObservation]:
        """Test helper: set page content directly instead of navigating."""
        page = self._new_page()
        page.set_content(html, wait_until="load")
        try:
            if variant_labels:
                obs: list[VariantObservation] = []
                for label in variant_labels:
                    try:
                        present = page.evaluate(_HAS_VARIANT_JS, label)
                    except Exception:
                        continue
                    if not present:
                        continue
                    try:
                        page.get_by_text(label, exact=True).first.click(timeout=2_000)
                    except Exception:
                        continue
                    page.wait_for_timeout(100)
                    raw = page.evaluate(_ATC_SCAN_JS)
                    obs.extend(self._observations_from_scan(raw, url=url, variant_label=label))
                if not obs:
                    raw = page.evaluate(_ATC_SCAN_JS)
                    obs.extend(self._observations_from_scan(raw, url=url, variant_label=None))
                return obs
            raw = page.evaluate(_ATC_SCAN_JS)
            return self._observations_from_scan(raw, url=url, variant_label=None)
        finally:
            page.close()
