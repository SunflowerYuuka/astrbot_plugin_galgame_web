import asyncio
import importlib
from pathlib import Path

PACKAGE_NAME = Path(__file__).resolve().parents[1].name
pipeline_module = importlib.import_module(f"{PACKAGE_NAME}.galgame_web.pipeline")


class _ChatQueue:
    def __init__(self):
        self.item = None

    async def put(self, item):
        self.item = item


class _BackQueue:
    async def get(self):
        return {"type": "end"}


class _QueueManager:
    def __init__(self):
        self.chat = _ChatQueue()
        self.back = _BackQueue()
        self.removed = []

    def get_or_create_back_queue(self, _msg_id, _wc_sid):
        return self.back

    def get_or_create_queue(self, _session_id):
        return self.chat

    def remove_back_queue(self, msg_id):
        self.removed.append(msg_id)


def test_pipeline_disables_reasoning_and_streaming(monkeypatch):
    manager = _QueueManager()
    monkeypatch.setattr(pipeline_module, "webchat_queue_mgr", manager)

    result = asyncio.run(
        pipeline_module.push_through_pipeline(
            {}, "tester", "a" * 32, "hello"
        )
    )

    assert result["text"] == ""
    payload = manager.chat.item[2]
    assert payload["enable_streaming"] is False
    assert payload["enable_reasoning"] is False
    assert payload["message"] == [{"type": "plain", "text": "hello"}]
    assert manager.removed
