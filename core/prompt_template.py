"""Placeholder-substitution engine for editable prompt templates.

See ``specs/prompts-ui-model-scoped-editing.md`` §5.3.

Templates use ``{{name}}`` tokens. Substitution is plain string replacement:
expressions are never evaluated, single braces are never touched, and an
unbalanced delimiter is rejected at save time instead of partially rendered.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Mapping, Sequence

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

SYSTEM_ALLOWLIST: Sequence[str] = (
    "book_title",
    "chapter_title",
    "toc_md",
    "book_metadata",
    "components",
)

CHAPTER_USER_ALLOWLIST: Sequence[str] = (
    "book_title",
    "chapter_title",
    "toc_md",
    "book_metadata",
    "target_words",
    "low",
    "high",
    "s_target",
    "source_md",
    "context_block",
    "format_instructions",
    "required_behavior",
)

COMPOSER_USER_ALLOWLIST: Sequence[str] = (
    "book_title",
    "chapter_summaries",
    "target_words",
    "low",
    "high",
    "s_target",
    "format_instructions",
)

ALLOWLISTS = {
    "chapter": {"system": SYSTEM_ALLOWLIST, "user": CHAPTER_USER_ALLOWLIST},
    "composer": {"system": SYSTEM_ALLOWLIST, "user": COMPOSER_USER_ALLOWLIST},
}

MAX_TEMPLATE_BYTES = 64 * 1024
MAX_VERSION_BYTES = 256 * 1024


class TemplateError(ValueError):
    """Base class for template validation/rendering errors."""


class UnbalancedTemplate(TemplateError):
    def __init__(self, text: str = ""):
        super().__init__("Unbalanced '{{' or '}}' delimiters in template")


class UnknownPlaceholder(TemplateError):
    def __init__(self, names: Iterable[str], allowlist: Iterable[str] = ()):
        self.names = list(dict.fromkeys(names))
        self.allowlist = list(allowlist)
        super().__init__("Unknown placeholder(s): " + ", ".join(self.names))


class MissingValue(TemplateError):
    def __init__(self, names: Iterable[str]):
        self.names = list(dict.fromkeys(names))
        super().__init__("Missing value(s) for placeholder(s): " + ", ".join(self.names))


def _is_blank(text: object) -> bool:
    return text is None or not str(text).strip()


def _stripped_braces(text: str) -> str:
    return PLACEHOLDER_RE.sub("", text)


def is_balanced(text: str) -> bool:
    remainder = _stripped_braces(text)
    return "{{" not in remainder and "}}" not in remainder


def extract_placeholders(text: object) -> List[str]:
    if text is None:
        return []
    seen: List[str] = []
    for match in PLACEHOLDER_RE.finditer(str(text)):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def validate(text: object, allowlist: Iterable[str]) -> List[str]:
    """Validate ``text`` against ``allowlist``. Returns the placeholder names.

    Blank templates are always valid. Raises ``UnbalancedTemplate`` for stray
    delimiters and ``UnknownPlaceholder`` for names outside the allowlist.
    """
    if _is_blank(text):
        return []
    raw = str(text)
    if not is_balanced(raw):
        raise UnbalancedTemplate(raw)
    names = extract_placeholders(raw)
    allowed = set(allowlist)
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise UnknownPlaceholder(unknown, allowlist)
    return names


def render(text: object, values: Mapping[str, object]) -> str:
    """Replace ``{{name}}`` tokens with ``values``; raises ``MissingValue``."""
    if text is None:
        return ""
    raw = str(text)
    if _is_blank(raw):
        return raw
    if not is_balanced(raw):
        raise UnbalancedTemplate(raw)
    names = extract_placeholders(raw)
    missing = [name for name in names if name not in values]
    if missing:
        raise MissingValue(missing)
    return PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), raw)
