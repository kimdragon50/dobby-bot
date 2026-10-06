import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_langfuse_client():
    config_module = types.ModuleType("src._shared.config")

    class Config:
        LANGFUSE_HOST = ""
        LANGFUSE_PUBLIC_KEY = ""
        LANGFUSE_SECRET_KEY = ""

    config_module.Config = Config

    logger_module = types.ModuleType("src._shared.logger")
    logger_module.logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )

    module_path = (
        Path(__file__).resolve().parents[1] / "src" / "_shared" / "langfuse_client.py"
    )
    spec = importlib.util.spec_from_file_location(
        "src._shared.langfuse_client_under_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "src._shared.config": config_module,
            "src._shared.logger": logger_module,
        },
    ):
        spec.loader.exec_module(module)
    return module


class FakeLangfuseClient:
    def __init__(self):
        self.score_kwargs = None
        self.flushed = False

    def create_score(self, **kwargs):
        self.score_kwargs = kwargs

    def flush(self):
        self.flushed = True


class CreateFeedbackScoreTest(unittest.TestCase):
    def test_uses_sdk_score_id(self):
        langfuse_client = load_langfuse_client()
        client = FakeLangfuseClient()

        with patch.object(langfuse_client, "get_client", return_value=client):
            result = langfuse_client.create_feedback_score(
                score_id="feedback-score-id",
                value=-1,
                session_id="slack-channel-thread",
                comment="reason: inaccurate",
                metadata={"source": "slack"},
            )

        self.assertTrue(result)
        self.assertEqual(
            client.score_kwargs,
            {
                "score_id": "feedback-score-id",
                "name": "user-feedback",
                "value": -1,
                "session_id": "slack-channel-thread",
                "data_type": "NUMERIC",
                "comment": "reason: inaccurate",
                "metadata": {"source": "slack"},
            },
        )
        self.assertTrue(client.flushed)


if __name__ == "__main__":
    unittest.main()
