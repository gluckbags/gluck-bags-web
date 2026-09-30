"""End-to-end tests for the public-site JavaScript (app/static/js/main.js).

Drives a real browser (Playwright sync API) against the seeded, HTTP-served app
(the `admin_live_server` fixture; public pages need no auth) to exercise the
landing-page interactions that only exist at runtime in the browser:

- the mobile hamburger menu (open/close + aria-expanded state),
- the sticky header toggling its `scrolled` class via an IntersectionObserver on
  a top sentinel, and
- the lazy background videos (.js-lazy-video) that defer injecting their <source>
  children until the section nears the viewport.

These behaviors are invisible to the test client (which runs no JS), so they're
covered here rather than in the request-level suites.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = pytest.mark.e2e

# Generous default: the page boots fonts/CSS/JS over a real HTTP server and the
# IntersectionObserver callbacks are async, so assertions must tolerate latency.
DEFAULT_TIMEOUT_MS = 30_000

PHONE_VIEWPORT = {"width": 390, "height": 800}
DESKTOP_VIEWPORT = {"width": 1280, "height": 900}


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    """A fresh, isolated browser context+page per test (no shared cookies/cache)."""
    context = browser.new_context()
    pg = context.new_page()
    pg.set_default_timeout(DEFAULT_TIMEOUT_MS)
    pg.set_default_navigation_timeout(DEFAULT_TIMEOUT_MS)
    try:
        yield pg
    finally:
        context.close()


def test_mobile_menu_opens_and_closes(admin_live_server: tuple[str, str], page: Page) -> None:
    """At a phone width the menu starts hidden; the toggle opens it (and sets
    aria-expanded=true); clicking a link inside runs closeMenu (hidden again,
    aria-expanded=false)."""
    base_url, _ = admin_live_server
    page.set_viewport_size(PHONE_VIEWPORT)
    page.goto(f"{base_url}/", wait_until="load")

    toggle = page.locator("#menuToggle")
    menu = page.locator("#mobileMenu")

    # Initial state: menu carries the `hidden` attribute (not rendered), toggle
    # advertises collapsed.
    expect(menu).to_be_hidden()
    expect(toggle).to_have_attribute("aria-expanded", "false")

    # Open: openMenu drops `hidden` and flips aria-expanded.
    toggle.click()
    expect(menu).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "true")

    # Clicking any link inside the menu must invoke closeMenu — verifies the
    # per-link click handler, not just a second toggle press.
    menu.get_by_role("link", name="Colección").click()
    expect(menu).to_be_hidden()
    expect(toggle).to_have_attribute("aria-expanded", "false")


def test_sticky_header_gains_scrolled_class_on_scroll(
    admin_live_server: tuple[str, str], page: Page
) -> None:
    """On desktop the header has no `scrolled` class at the top; scrolling past
    the 40px sentinel makes the IntersectionObserver add it."""
    base_url, _ = admin_live_server
    page.set_viewport_size(DESKTOP_VIEWPORT)
    page.goto(f"{base_url}/", wait_until="load")

    header = page.locator("#header")

    # At the very top the sentinel is on-screen, so the header is transparent.
    expect(header).not_to_have_class("scrolled")

    # Scroll well past the 40px sentinel; the observer then flips the class.
    page.mouse.wheel(0, 600)

    # IntersectionObserver fires asynchronously; poll for the class with a clear
    # failure message if it never lands.
    page.wait_for_function(
        "() => document.getElementById('header').classList.contains('scrolled')",
        timeout=DEFAULT_TIMEOUT_MS,
    )
    expect(header).to_have_class(re.compile(r"\bscrolled\b"))


def test_lazy_background_video_loads_when_scrolled_into_view(
    admin_live_server: tuple[str, str], page: Page
) -> None:
    """A .js-lazy-video has no <source> children until it nears the viewport.
    Scrolling it into view must trigger loadVideoSources: dataset.loaded becomes
    '1' and at least one <source> is injected."""
    base_url, _ = admin_live_server
    page.set_viewport_size(DESKTOP_VIEWPORT)
    page.goto(f"{base_url}/", wait_until="load")

    lazy_videos = page.locator(".js-lazy-video")
    if lazy_videos.count() == 0:
        pytest.skip("no .js-lazy-video elements on the page to exercise lazy loading")

    target = lazy_videos.first

    # Precondition: it really is lazy — no sources and not yet loaded before it
    # enters the viewport. (Asserting this guards against a false positive where
    # the markup already shipped <source> children.)
    assert target.evaluate("v => v.querySelectorAll('source').length") == 0
    assert target.evaluate("v => v.dataset.loaded || ''") == ""

    # Bring it into view; the observer's 200px rootMargin then loads it.
    target.scroll_into_view_if_needed()

    page.wait_for_function(
        """(v) => v.dataset.loaded === '1' && v.querySelectorAll('source').length > 0""",
        arg=target.element_handle(),
        timeout=DEFAULT_TIMEOUT_MS,
    )

    assert target.evaluate("v => v.dataset.loaded") == "1"
    assert target.evaluate("v => v.querySelectorAll('source').length") > 0


def _badge_opacity(page: Page) -> float:
    return float(
        page.locator("#shop .product-badge").first.evaluate("el => getComputedStyle(el).opacity")
    )


def test_home_product_card_opens_detail_with_a_single_tap(
    admin_live_server: tuple[str, str], browser: Browser
) -> None:
    """On touch, a :hover that reveals the card badge made iOS Safari spend the
    first tap on the hover state, so the image needed two taps. The reveal must
    not apply without a real pointer, and one tap on the image opens the PDP."""
    base_url, _ = admin_live_server
    context = browser.new_context(viewport=PHONE_VIEWPORT, has_touch=True, is_mobile=True)
    page = context.new_page()
    page.set_default_timeout(DEFAULT_TIMEOUT_MS)
    try:
        page.goto(f"{base_url}/", wait_until="load")
        assert page.evaluate("matchMedia('(hover: hover)').matches") is False

        card = page.locator("#shop .product").first
        href = card.get_attribute("href")
        card.scroll_into_view_if_needed()
        card.hover()
        assert _badge_opacity(page) == 0

        card.locator(".product-media img").tap()
        expect(page).to_have_url(f"{base_url}{href}")
    finally:
        context.close()


def test_home_product_card_hover_still_reveals_badge_on_desktop(
    admin_live_server: tuple[str, str], page: Page
) -> None:
    base_url, _ = admin_live_server
    page.set_viewport_size(DESKTOP_VIEWPORT)
    page.goto(f"{base_url}/", wait_until="load")

    card = page.locator("#shop .product").first
    card.scroll_into_view_if_needed()
    card.hover()
    # Waits out the 0.5s opacity transition.
    page.wait_for_function(
        "getComputedStyle(document.querySelector('#shop .product-badge')).opacity === '1'"
    )
