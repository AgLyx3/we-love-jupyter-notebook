"""The tab fetches nothing from the internet.

The editor binds to loopback and says so on its front page, but the interface
serving that promise used to `<link>` three stylesheets on fonts.googleapis.com.
Two things followed: Google saw a request, with the visitor's IP, every time
the tab opened; and on a host that could not reach it — offline, air-gapped, a
corporate network that blocks the CDN — Material Symbols never arrived and
every icon rendered as its own ligature name, which took the controls with it
(#51).

The fonts are vendored now (`scripts/vendor-fonts.mjs`). These tests are here
because the failure is invisible to anyone developing with a network: a link
added back looks perfectly fine on the machine that adds it, and only breaks
for the person who most wanted a local tool.

What is checked is the *fetchable* positions — `href`/`src` in the HTML, and
`url()`/`@import` in the CSS — not every http string in the bundle. A namespace
literal like `http://www.w3.org/2000/svg` inside an inline SVG is not a
request, and failing on it would teach the next person to delete the test. What
the browser actually asks for at runtime is covered from the other side, by
`e2e/offline.spec.ts`.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from backend.app.bundled import candidate_dist_dirs

REPO_ROOT = Path(__file__).resolve().parents[2]

# `src="//fonts.googleapis.com/..."` inherits the page's scheme and is as
# external as an absolute one, so protocol-relative counts too.
ABSOLUTE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)
HTML_REFERENCES = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
CSS_REFERENCES = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.IGNORECASE)

LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}


def external_targets(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Every reference in `text` that would leave this machine."""
    found = []
    for reference in pattern.findall(text):
        target = reference.strip()
        if not ABSOLUTE.match(target):
            continue  # relative, or a data:/blob: URI — same origin or inline
        if (urlsplit(target).hostname or "") not in LOOPBACK:
            found.append(target)
    return found


def built_bundle() -> Path | None:
    """The real `npm run build` output, if this checkout has one."""
    for candidate in candidate_dist_dirs():
        if (candidate / "index.html").is_file():
            return candidate
    return None


def test_the_source_page_links_nothing_external():
    """index.html is where the CDN links were, and where they would come back.

    This one needs no build, so it runs in every checkout — including the ones
    where the failure would otherwise wait until someone packaged a wheel.
    """
    page = (REPO_ROOT / "index.html").read_text(encoding="utf-8")
    assert external_targets(page, HTML_REFERENCES) == []


def test_the_built_bundle_references_no_external_origin():
    """Whatever Vite pulled in, none of it is fetched from somewhere else.

    A dependency's stylesheet carrying its own `@import url(https://...)` would
    never touch index.html and would still put the tab on the network.
    """
    bundle = built_bundle()
    if bundle is None:
        pytest.skip("no built frontend; run `npm run build`")

    offenders: dict[str, list[str]] = {}
    for path in sorted(bundle.rglob("*")):
        if path.suffix.lower() not in {".html", ".css"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        pattern = HTML_REFERENCES if path.suffix.lower() == ".html" else CSS_REFERENCES
        found = external_targets(text, pattern)
        # A CSS file can also carry `@import "https://..."` without url().
        if path.suffix.lower() == ".css":
            found += external_targets(
                "\n".join(re.findall(r"@import\s+([^;]+);", text)),
                re.compile(r"""["']([^"']+)["']"""),
            )
        if found:
            offenders[str(path.relative_to(bundle))] = found

    assert offenders == {}


def test_the_fonts_are_actually_in_the_bundle():
    """A page that links nothing external and ships no fonts is also 'clean'.

    Deleting the `<link>`s alone would pass the two tests above and leave the
    icons exactly as broken as #51 describes, so this asserts the replacement
    arrived: the subsetted icon font is the one that has to be there.
    """
    bundle = built_bundle()
    if bundle is None:
        pytest.skip("no built frontend; run `npm run build`")

    fonts = [path.name for path in bundle.rglob("*.woff2")]
    assert any(name.startswith("material-symbols-outlined") for name in fonts), fonts
    assert any(name.startswith("inter-latin") for name in fonts), fonts
