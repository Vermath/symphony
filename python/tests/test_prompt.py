import unittest

from symphony_service.errors import TemplateParseError, TemplateRenderError
from symphony_service.models import Issue
from symphony_service.prompt import render_prompt


class PromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issue = Issue(
            id="1",
            identifier="ABC-1",
            title="Fix bug",
            description="Broken flow",
            priority=1,
            state="Todo",
        )

    def test_render_prompt_success(self) -> None:
        text = render_prompt("Issue {{ issue.identifier }} / attempt {{ attempt }}", self.issue, attempt=2)
        self.assertEqual("Issue ABC-1 / attempt 2", text)

    def test_unknown_variable_raises(self) -> None:
        with self.assertRaises(TemplateRenderError):
            render_prompt("{{ issue.missing_field }}", self.issue, attempt=None)

    def test_unknown_filter_raises_parse_error(self) -> None:
        with self.assertRaises(TemplateParseError):
            render_prompt("{{ issue.identifier | does_not_exist }}", self.issue, attempt=None)


if __name__ == "__main__":
    unittest.main()

