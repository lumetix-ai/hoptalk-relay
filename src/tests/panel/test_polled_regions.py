"""Every region that htmx polls on a timer morphs the answer in, so that a poll keeps the operator's text selection.

The banner region is the exception: it is swapped only when its answer changed (polled_regions.js).
"""

import re
from pathlib import Path

import panel

TEMPLATE_DIRECTORY = Path(panel.__file__).parent / "templates"
OPENING_TAG_PATTERN = re.compile(r"<[a-z]+\b[^>]*>")
TIMED_POLL_PATTERN = re.compile(r'hx-trigger="[^"]*\bevery \d+s')
BANNER_REGION_ATTRIBUTE = 'id="banner-region"'
MORPH_ATTRIBUTES = ('hx-ext="morph"', 'hx-swap="morph:outerHTML"')


def find_timed_poll_tags() -> dict[str, str]:
    """The opening tag of every region polled on a timer, keyed by its template and line."""
    timed_poll_tags = {}
    for template_path in sorted(TEMPLATE_DIRECTORY.rglob("*.html")):
        template_text = template_path.read_text()
        for opening_tag_match in OPENING_TAG_PATTERN.finditer(template_text):
            opening_tag = opening_tag_match.group(0)
            if TIMED_POLL_PATTERN.search(opening_tag) and BANNER_REGION_ATTRIBUTE not in opening_tag:
                line_number = template_text.count("\n", 0, opening_tag_match.start()) + 1
                timed_poll_tags[f"{template_path.relative_to(TEMPLATE_DIRECTORY)}:{line_number}"] = opening_tag
    return timed_poll_tags


def test_every_region_polled_on_a_timer_morphs_the_answer_in() -> None:
    timed_poll_tags = find_timed_poll_tags()

    assert any(location.startswith("panel/node.html:") for location in timed_poll_tags)
    regions_that_do_not_morph = [
        location
        for location, opening_tag in timed_poll_tags.items()
        if not all(morph_attribute in opening_tag for morph_attribute in MORPH_ATTRIBUTES)
    ]
    assert regions_that_do_not_morph == []
