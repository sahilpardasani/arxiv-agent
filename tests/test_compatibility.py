import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jobs
import legacy
import publishing


class _FakeScheduler:
    def __init__(self, **kwargs): self.started = False
    def add_job(self, *args, **kwargs): pass
    def start(self): self.started = True
    def shutdown(self, wait=False): self.started = False


class CompatibilityTests(unittest.TestCase):
    def test_no_redis_selects_legacy_scheduler_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(legacy.legacy_mode_enabled())
        with patch.dict(os.environ, {"ENABLE_LEGACY_MODE": "false"}, clear=True):
            self.assertFalse(legacy.legacy_mode_enabled())

    def test_redis_always_selects_scalable_services(self):
        with patch.dict(os.environ, {"REDIS_URL": "redis://example", "ENABLE_LEGACY_MODE": "true"}, clear=True):
            self.assertFalse(legacy.legacy_mode_enabled())

    def test_no_redis_starts_one_elected_scheduler(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
             patch.object(legacy, "BackgroundScheduler", _FakeScheduler):
            runtime = legacy.LegacyRuntime(Path(directory))
            self.assertTrue(runtime.start())
            self.assertTrue(runtime.scheduler.started)
            self.assertFalse(runtime.start())
            runtime.stop()

    def test_redis_configuration_does_not_start_legacy_scheduler(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"REDIS_URL": "redis://example"}, clear=True):
            runtime = legacy.LegacyRuntime(Path(directory))
            self.assertFalse(runtime.start())
            self.assertIsNone(runtime.scheduler)

    def test_successful_pipeline_invokes_publish_with_data_dir(self):
        result = SimpleNamespace(returncode=0, stdout="completed", stderr="")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"DATA_DIR": directory}, clear=False),
            patch.object(jobs.subprocess, "run", return_value=result),
            patch.object(jobs, "publish_outputs") as publish,
        ):
            jobs.execute_pipeline({"id": "test"})
        publish.assert_called_once_with("completed", directory)

    def test_safety_guard_skips_github_publish(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_REPO": "owner/repo"}, clear=False),
            patch.object(publishing, "_publish_one") as publish_one,
        ):
            published = publishing.publish_outputs("SAFETY GUARD: zero papers", Path(directory))
        self.assertFalse(published)
        publish_one.assert_not_called()

    def test_publish_reads_data_dir_but_uses_repository_names(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_REPO": "owner/repo"}, clear=False),
            patch.object(publishing, "_publish_one") as publish_one,
        ):
            self.assertTrue(publishing.publish_outputs("completed", Path(directory)))
        self.assertEqual([call.args[1] for call in publish_one.call_args_list], list(publishing.OUTPUT_NAMES))
        self.assertTrue(all(call.args[0] == Path(directory).resolve() for call in publish_one.call_args_list))


if __name__ == "__main__": unittest.main()
