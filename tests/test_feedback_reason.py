import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def _module(name, path, stubs):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


def _logger_module():
    module = types.ModuleType("src._shared.logger")
    module.logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    return module


def load_feedback_handler():
    config = types.ModuleType("src._shared.config")
    config.Config = types.SimpleNamespace(FEEDBACK_PENDING_TABLE="pending")

    langfuse_client = types.ModuleType("src._shared.langfuse_client")
    langfuse_client.create_feedback_score = lambda **kwargs: True

    store = types.ModuleType("src.feedback.store")
    store.PendingFeedbackStore = lambda table_name: None

    return _module(
        "feedback_handler_under_test",
        "src/feedback/handler.py",
        {
            "src._shared.config": config,
            "src._shared.langfuse_client": langfuse_client,
            "src._shared.logger": _logger_module(),
            "src.feedback.store": store,
        },
    )


def load_mention_handler():
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *args, **kwargs: None

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **kwargs: kwargs

    slack_sdk = types.ModuleType("slack_sdk")
    slack_errors = types.ModuleType("slack_sdk.errors")
    slack_errors.SlackApiError = Exception

    config = types.ModuleType("src._shared.config")
    config.Config = types.SimpleNamespace(AGENTCORE_SUPERVISOR_ARN="runtime")

    return _module(
        "mention_handler_under_test",
        "src/mention/handler.py",
        {
            "boto3": boto3,
            "botocore": botocore,
            "botocore.config": botocore_config,
            "slack_sdk": slack_sdk,
            "slack_sdk.errors": slack_errors,
            "src._shared.config": config,
            "src._shared.logger": _logger_module(),
        },
    )


class _Store:
    def __init__(self):
        self.deleted = []
        self.pending = {
            "score_id": "score-id",
            "session_id": "slack-channel-root",
            "answer_ts": "answer-ts",
        }

    def has_hint(self, thread_key):
        return True

    def get(self, thread_key):
        return self.pending

    def delete(self, thread_key):
        self.deleted.append(thread_key)


class _App:
    def __init__(self):
        self.handlers = {}

    def event(self, event_name):
        def register(handler):
            self.handlers[event_name] = handler
            return handler

        return register


class FeedbackReasonTest(unittest.TestCase):
    def test_tagged_feedback_is_saved_without_bot_mention_or_prefix(self):
        module = load_feedback_handler()
        module.create_feedback_score = Mock(return_value=True)
        handler = module.FeedbackHandler.__new__(module.FeedbackHandler)
        handler.bot_user_id = "BOT"
        handler.store = _Store()
        client = types.SimpleNamespace(reactions_add=Mock())

        consumed = handler.try_consume_reply(
            {
                "channel": "channel",
                "thread_ts": "root",
                "ts": "reply",
                "user": "USER",
                "text": "<@BOT> 피드백: 표가 깨져요",
            },
            client,
        )

        self.assertTrue(consumed)
        self.assertEqual(handler.store.deleted, ["channel#root#USER"])
        self.assertEqual(
            module.create_feedback_score.call_args.kwargs["comment"],
            "answer_ts=answer-ts\nreason: 표가 깨져요",
        )
        client.reactions_add.assert_called_once_with(
            channel="channel", timestamp="reply", name="white_check_mark"
        )

    def test_failed_score_write_keeps_pending_reason(self):
        module = load_feedback_handler()
        module.create_feedback_score = Mock(return_value=False)
        handler = module.FeedbackHandler.__new__(module.FeedbackHandler)
        handler.bot_user_id = "BOT"
        handler.store = _Store()
        client = types.SimpleNamespace(reactions_add=Mock())

        consumed = handler.try_consume_reply(
            {
                "channel": "channel",
                "thread_ts": "root",
                "ts": "reply",
                "user": "USER",
                "text": "<@BOT> 피드백: 다시 시도할 사유",
            },
            client,
        )

        self.assertTrue(consumed)
        self.assertEqual(handler.store.deleted, [])
        client.reactions_add.assert_not_called()

    def test_app_mention_interceptor_runs_before_agent_dispatch(self):
        module = load_mention_handler()
        handler = module.MentionHandler.__new__(module.MentionHandler)
        handler._dispatch = Mock()
        app = _App()
        interceptor = Mock(return_value=True)

        handler.register_handlers(app, message_interceptors=[interceptor])
        event = {"channel": "channel", "thread_ts": "root", "user": "USER"}
        client = object()
        say = object()
        app.handlers["app_mention"](event, client, say)

        interceptor.assert_called_once_with(event, client)
        handler._dispatch.assert_not_called()

    def test_mention_uses_temporary_eyes_reaction_instead_of_ack_message(self):
        module = load_mention_handler()
        handler = module.MentionHandler.__new__(module.MentionHandler)
        response = types.SimpleNamespace(read=Mock(return_value=b"answer"))
        handler.agentcore = types.SimpleNamespace(
            invoke_agent_runtime=Mock(return_value={"response": response})
        )
        handler.supervisor_arn = "runtime"
        handler._on_answer = None
        client = types.SimpleNamespace(
            reactions_add=Mock(),
            reactions_remove=Mock(),
        )
        say = Mock(return_value={"ts": "answer-ts"})

        handler._process(
            {
                "channel": "channel",
                "ts": "request-ts",
                "user": "USER",
                "text": "<@BOT> 질문",
            },
            client,
            say,
        )

        client.reactions_add.assert_called_once_with(
            channel="channel", timestamp="request-ts", name="eyes"
        )
        client.reactions_remove.assert_called_once_with(
            channel="channel", timestamp="request-ts", name="eyes"
        )
        say.assert_called_once_with(text="answer", thread_ts="request-ts")

    def test_mention_removes_eyes_reaction_when_agentcore_fails(self):
        module = load_mention_handler()
        handler = module.MentionHandler.__new__(module.MentionHandler)
        handler.agentcore = types.SimpleNamespace(
            invoke_agent_runtime=Mock(side_effect=RuntimeError("failed"))
        )
        handler.supervisor_arn = "runtime"
        handler._on_answer = None
        client = types.SimpleNamespace(
            reactions_add=Mock(),
            reactions_remove=Mock(),
        )
        say = Mock()

        handler._process(
            {
                "channel": "channel",
                "ts": "request-ts",
                "user": "USER",
                "text": "<@BOT> 질문",
            },
            client,
            say,
        )

        client.reactions_remove.assert_called_once_with(
            channel="channel", timestamp="request-ts", name="eyes"
        )
        say.assert_called_once_with(
            text="죄송해요, 잠시 문제가 생겼어요. 다시 시도해 주세요.",
            thread_ts="request-ts",
        )


if __name__ == "__main__":
    unittest.main()
