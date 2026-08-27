import json
import os
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pipeline_config import (
    DEFAULT_GROQ_MODEL,
    PipelineConfigurationError,
    analysis_prompt_template,
    groq_model,
    load_conference_catalog,
    render_analysis_prompt,
)


class PipelineConfigurationTests(unittest.TestCase):
    def test_requested_qwen_model_is_the_default_and_env_can_override_it(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(groq_model(), "qwen/qwen3.8-27b")
            self.assertEqual(DEFAULT_GROQ_MODEL, "qwen/qwen3.8-27b")
        with patch.dict(os.environ, {"GROQ_MODEL": "vendor/replacement-model"}, clear=True):
            self.assertEqual(groq_model(), "vendor/replacement-model")

    def test_invalid_model_identifier_is_rejected(self):
        with patch.dict(os.environ, {"GROQ_MODEL": "https://example.test/model?token=x"}, clear=True):
            with self.assertRaises(PipelineConfigurationError):
                groq_model()

    def test_default_prompt_contains_required_fields_and_json_schema(self):
        with patch.dict(os.environ, {}, clear=True):
            prompt = analysis_prompt_template()
        for expected in ("${title}", "${summary}", "problem_statement", "technical_breakdown"):
            self.assertIn(expected, prompt)

    def test_custom_prompt_substitutes_untrusted_text_as_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.txt"
            path.write_text('Title=${title}\nSummary=${summary}\nLiteral JSON: {"ok": true}', encoding="utf-8")
            with patch.dict(os.environ, {"ANALYSIS_PROMPT_FILE": str(path)}, clear=True):
                rendered = render_analysis_prompt({"title": "$comment ${unknown}", "summary": "safe"})
        self.assertIn("$comment ${unknown}", rendered)
        self.assertIn('{"ok": true}', rendered)

    def test_unknown_prompt_placeholder_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.txt"
            path.write_text("${title} ${summary} ${secret}", encoding="utf-8")
            with patch.dict(os.environ, {"ANALYSIS_PROMPT_FILE": str(path)}, clear=True):
                with self.assertRaises(PipelineConfigurationError):
                    analysis_prompt_template()

    def test_absent_conference_file_preserves_defaults(self):
        defaults = {"Field": {"CONF", "Conference Name"}}
        ranks = {"CONF": "A+"}
        with patch.dict(os.environ, {}, clear=True):
            loaded_categories, loaded_ranks = load_conference_catalog(defaults, ranks)
        self.assertEqual(loaded_categories, defaults)
        self.assertEqual(loaded_ranks, ranks)
        self.assertIsNot(loaded_categories, defaults)

    def test_valid_conference_file_replaces_catalog(self):
        document = {"categories": {"New Field": ["NEWCONF", "New Conference"]}, "ranks": {"NEWCONF": "A"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conferences.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with patch.dict(os.environ, {"CONFERENCE_CONFIG_FILE": str(path)}, clear=True):
                categories, ranks = load_conference_catalog({"Old": {"OLD"}}, {"OLD": "B"})
        self.assertEqual(categories, {"New Field": {"NEWCONF", "New Conference"}})
        self.assertEqual(ranks, {"NEWCONF": "A"})

    def test_invalid_conference_rank_fails_closed(self):
        document = {"categories": {"Field": ["CONF"]}, "ranks": {"CONF": "S"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conferences.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with patch.dict(os.environ, {"CONFERENCE_CONFIG_FILE": str(path)}, clear=True):
                with self.assertRaises(PipelineConfigurationError):
                    load_conference_catalog({}, {})


class GroqConfigurationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("GROQ_API_KEY", "test-key")
        import arxiv_agent
        cls.agent = arxiv_agent

    def test_analysis_call_uses_configured_qwen_and_json_mode(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"confidence":"high"}'))])
        paper = {"title": "Title", "arxiv_id": "1234.5678", "comment": "Accepted", "summary": "Summary"}
        with patch.object(self.agent.client.chat.completions, "create", return_value=response) as create:
            result = self.agent.generate_paper_analysis(paper)
        self.assertEqual(result, {"confidence": "high"})
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["model"], "qwen/qwen3.8-27b")
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["reasoning_effort"], "none")
        self.assertEqual(kwargs["messages"][0]["role"], "system")

    def test_permanent_model_error_fails_after_one_call(self):
        class ModelNotFound(Exception):
            status_code = 404

        paper = {"title": "Title", "arxiv_id": "1234.5678", "comment": "Accepted", "summary": "Summary"}
        with patch.object(
            self.agent.client.chat.completions,
            "create",
            side_effect=ModelNotFound(),
        ) as create:
            with self.assertRaises(self.agent.PermanentAnalysisError):
                self.agent.generate_paper_analysis(paper)
        create.assert_called_once()

    def test_builtin_conference_matching_remains_compatible(self):
        info = self.agent.extract_conference_info({"comment": "Accepted to ICML 2026"})
        self.assertEqual(info["conference"], "ICML")
        self.assertEqual(info["category"], "General ML/AI")
        self.assertEqual(info["rank"], "A+")

    def test_zero_result_safety_guard_does_not_touch_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "papers.json"
            archive = Path(directory) / "papers_archive.json"
            current.write_text("current-sentinel", encoding="utf-8")
            archive.write_text("archive-sentinel", encoding="utf-8")
            result = self.agent.save_results(([], None), str(current), str(archive))
            self.assertEqual(result, (None, None))
            self.assertEqual(current.read_text(encoding="utf-8"), "current-sentinel")
            self.assertEqual(archive.read_text(encoding="utf-8"), "archive-sentinel")

    def test_oversized_arxiv_response_is_rejected_before_parsing(self):
        response = SimpleNamespace(content=SimpleNamespace(read=AsyncMock(return_value=b"12345")), charset="utf-8")
        with patch.object(self.agent, "MAX_ARXIV_RESPONSE_BYTES", 4), self.assertRaises(ValueError):
            asyncio.run(self.agent.read_arxiv_response(response))

    def test_arxiv_response_reads_every_network_chunk(self):
        reader = AsyncMock(side_effect=[b"<feed>", b"complete", b"</feed>", b""])
        response = SimpleNamespace(content=SimpleNamespace(read=reader), charset="utf-8")
        result = asyncio.run(self.agent.read_arxiv_response(response))
        self.assertEqual(result, "<feed>complete</feed>")
        self.assertEqual(reader.call_count, 4)


if __name__ == "__main__":
    unittest.main()
