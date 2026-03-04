"""Prompt template compilation and rendering."""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from jinja2 import Environment, StrictUndefined, Template, TemplateAssertionError, TemplateSyntaxError
from jinja2.exceptions import UndefinedError

from .errors import TemplateParseError, TemplateRenderError
from .models import Issue

_JINJA_ENV = Environment(undefined=StrictUndefined, autoescape=False, trim_blocks=False, lstrip_blocks=False)


@lru_cache(maxsize=128)
def _compile(template_text: str) -> Template:
    try:
        return _JINJA_ENV.from_string(template_text)
    except (TemplateSyntaxError, TemplateAssertionError) as exc:
        raise TemplateParseError(f"template_parse_error: {exc}") from exc


def render_prompt(template_text: str, issue: Issue, attempt: Optional[int]) -> str:
    template = _compile(template_text)
    try:
        return template.render(issue=issue.template_payload(), attempt=attempt)
    except (UndefinedError, TypeError, ValueError) as exc:
        raise TemplateRenderError(f"template_render_error: {exc}") from exc

