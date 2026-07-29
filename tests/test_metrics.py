import json
import tempfile
import unittest
from pathlib import Path

from ralph_loop.agent import parse_usage_log
from ralph_loop.journal import Journal
from ralph_loop.ui import UI


class MetricsTests(unittest.TestCase):
    def test_parse_usage_log_uses_final_jsonl_usage_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.log"
            path.write_text(
                "{" + '"type":"turn.completed","usage":{"input_tokens":1200,'
                '"output_tokens":34,"cached_input_tokens":900}}' + "\n",
                encoding="utf-8",
            )
            usage = parse_usage_log(path)
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cached_input_tokens), (1200, 34, 900))

    def test_work_metrics_combines_legacy_time_new_time_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory))
            journal.path.write_text(
                "\n".join(
                    json.dumps(entry)
                    for entry in [
                        {"timestamp": "2026-01-01T00:00:00+00:00", "event": "role_started", "taskId": "A", "runId": "planner-001"},
                        {"timestamp": "2026-01-01T00:00:03+00:00", "event": "role_accepted", "taskId": "A", "runId": "planner-001"},
                        {"timestamp": "2026-01-01T00:00:04+00:00", "event": "role_accepted", "taskId": "B", "runId": "reviewer-001", "elapsedSeconds": 2.5, "usage": {"inputTokens": 100, "outputTokens": 20, "cachedInputTokens": 70}},
                        {"timestamp": "2026-01-01T00:00:05+00:00", "event": "quality_gates_passed", "taskId": "B", "elapsedSeconds": 1.5},
                    ]
                ) + "\n",
                encoding="utf-8",
            )
            metrics = journal.work_metrics({"A": "docs/a.md", "B": "docs/b.md"})
        self.assertEqual(metrics.active_seconds, 7.0)
        self.assertEqual(metrics.task_seconds["A"], 3.0)
        self.assertEqual(metrics.prd_seconds["docs/b.md"], 4.0)
        self.assertTrue(metrics.usage.available)
        self.assertEqual(metrics.usage.input_tokens, 100)

    def test_summary_shows_progress_time_and_usage(self):
        output = []
        ui = UI(color="never")
        from unittest.mock import patch

        with patch("builtins.print", side_effect=lambda *args, **kwargs: output.append(" ".join(map(str, args)))):
            ui.summary(
                2,
                4,
                0,
                1,
                active_seconds=65,
                total_usage={"available": True, "input_tokens": 1200, "output_tokens": 30, "cached_input_tokens": 900},
                all_complete=False,
            )
        rendered = "\n".join(output)
        self.assertIn("2/4 completed · 50%", rendered)
        self.assertIn("Active work: 1m 05s", rendered)
        self.assertIn("1k input · 30 output · 900 cached", rendered)
