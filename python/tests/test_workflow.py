import tempfile
import unittest
from pathlib import Path

from symphony_service.errors import WorkflowError
from symphony_service.workflow import WorkflowStore, load_workflow


class WorkflowTests(unittest.TestCase):
    def test_load_workflow_with_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(
                "---\ntracker:\n  kind: linear\n  project_slug: demo\n---\n\nHello {{ issue.identifier }}\n",
                encoding="utf-8",
            )

            workflow = load_workflow(path)
            self.assertEqual("linear", workflow.config["tracker"]["kind"])
            self.assertEqual("Hello {{ issue.identifier }}", workflow.prompt_template)

    def test_non_map_front_matter_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text("---\n- not\n- a\n- map\n---\nBody", encoding="utf-8")
            with self.assertRaises(WorkflowError):
                load_workflow(path)

    def test_store_keeps_last_good_on_invalid_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text("---\ntracker:\n  kind: linear\n---\nGood", encoding="utf-8")
            store = WorkflowStore(path)
            initial = store.load_initial()
            self.assertEqual("Good", initial.prompt_template)

            path.write_text("---\ntracker:\n  kind: [invalid\n---\nBad", encoding="utf-8")
            snapshot = store.refresh()
            self.assertFalse(snapshot.changed)
            self.assertIsNotNone(snapshot.error)
            self.assertEqual("Good", snapshot.workflow.prompt_template)


if __name__ == "__main__":
    unittest.main()

