"""Secondary text in the panel's templates stays readable in both colour schemes.

slate-400 on white reaches about 2.6:1 and slate-500 on the dark cards about 3.7:1, both below
the 4.5:1 that small text needs. Icons are drawn through template tags and are left out.
"""

import re
from pathlib import Path

import panel

TEMPLATE_DIRECTORY = Path(panel.__file__).parent / "templates"
CLASS_ATTRIBUTE_PATTERN = re.compile(r'(?<![\w:-])class="([^"]*)"')


def is_inside_template_tag(template_text: str, position: int) -> bool:
    return template_text.rfind("{%", 0, position) > template_text.rfind("%}", 0, position)


def find_low_contrast_text_classes() -> list[str]:
    findings = []
    for template_path in sorted(TEMPLATE_DIRECTORY.rglob("*.html")):
        template_text = template_path.read_text()
        for class_match in CLASS_ATTRIBUTE_PATTERN.finditer(template_text):
            if is_inside_template_tag(template_text, class_match.start()):
                continue
            class_names = class_match.group(1).split()
            has_dark_text_colour = any(class_name.startswith("dark:text-") for class_name in class_names)
            if "text-slate-400" in class_names or ("text-slate-500" in class_names and not has_dark_text_colour):
                line_number = template_text.count("\n", 0, class_match.start()) + 1
                findings.append(f"{template_path.relative_to(TEMPLATE_DIRECTORY)}:{line_number}")
    return findings


def test_secondary_text_has_enough_contrast_in_both_colour_schemes() -> None:
    assert find_low_contrast_text_classes() == []
