import os
import sys
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config_utils import resolve_app_version, resolve_workflow_build_state


class AppVersionTests(unittest.TestCase):
    def test_main_build_includes_commit(self):
        self.assertEqual(resolve_app_version("main", "4020be2abcdef"), "main-4020be2")

    def test_ci_generated_build_version_is_preserved(self):
        self.assertEqual(resolve_app_version("main-4020be2", "4020be2abcdef"), "main-4020be2")

    def test_release_tag_is_preserved(self):
        self.assertEqual(resolve_app_version("v1.3.0", "4020be2abcdef"), "v1.3.0")

    def test_local_build_has_explicit_development_version(self):
        self.assertEqual(resolve_app_version("dev", "dev"), "development")


class WorkflowBuildStateTests(unittest.TestCase):
    def test_successful_target_build_is_ready(self):
        state = resolve_workflow_build_state(
            [{
                "head_sha": "abc123",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/example/actions/runs/1",
            }],
            "abc123",
        )

        self.assertTrue(state["image_ready"])
        self.assertEqual(state["image_build_status"], "completed")

    def test_running_or_failed_build_is_not_ready(self):
        for status, conclusion in (("in_progress", ""), ("completed", "failure")):
            with self.subTest(status=status, conclusion=conclusion):
                state = resolve_workflow_build_state(
                    [{"head_sha": "abc123", "status": status, "conclusion": conclusion}],
                    "abc123",
                )
                self.assertFalse(state["image_ready"])

    def test_missing_target_build_is_not_ready(self):
        state = resolve_workflow_build_state([], "abc123")

        self.assertFalse(state["image_ready"])
        self.assertEqual(state["image_build_status"], "not_found")


if __name__ == "__main__":
    unittest.main()
