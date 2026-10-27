"""微信适配器的测试：全部走 FakeDriver，不碰真实微信窗口。"""

from __future__ import annotations

import asyncio
import threading
import unittest

from gateway.adapters import wechat
from gateway.models import ChatKind, InboundMessage, OutboundMessage, Platform, SendResult


class FakeDriver:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.started = False
        self.stopped = False
        self._release = threading.Event()
        self.on_message = None
        self.fail_with: Exception | None = None

    def start(self, on_message) -> None:
        self.started = True
        self.on_message = on_message
        if self.fail_with is not None:
            raise self.fail_with
        self._release.wait(timeout=5)   # 模拟 KeepRunning 阻塞

    def send(self, chat_id: str, text: str) -> SendResult:
        self.sent.append((chat_id, text))
        return SendResult(ok=True, msg_id=f"wx-{len(self.sent)}")

    def stop(self) -> None:
        self.stopped = True
        self._release.set()


class TestPush(unittest.TestCase):
    def test_push_converts_to_inbound(self) -> None:
        driver = FakeDriver()
        adapter = wechat.WeChatAdapter({}, driver=driver)
        got: list[InboundMessage] = []
        adapter.bind(lambda m: asyncio.sleep(0, result=got.append(m)))

        async def scenario() -> None:
            adapter._loop = asyncio.get_running_loop()
            adapter._push("张三", "在吗")
            await asyncio.sleep(0.05)

        asyncio.run(scenario())
        # 没有 start() 消费队列，所以这里只验证入队
        self.assertEqual(adapter._queue.qsize(), 1)
        queued = adapter._queue.get_nowait()
        self.assertEqual(queued.platform, Platform.WECHAT)
        self.assertEqual(queued.chat_id, "张三")
        self.assertEqual(queued.text, "在吗")
        self.assertEqual(queued.kind, ChatKind.PRIVATE)

    def test_group_chatroom_detected(self) -> None:
        adapter = wechat.WeChatAdapter({}, driver=FakeDriver())

        async def scenario() -> None:
            adapter._loop = asyncio.get_running_loop()
            adapter._push("某某群@chatroom", "大家早")

        asyncio.run(scenario())
        self.assertEqual(adapter._queue.get_nowait().kind, ChatKind.GROUP)

    def test_push_before_start_is_dropped_safely(self) -> None:
        adapter = wechat.WeChatAdapter({}, driver=FakeDriver())
        adapter._push("张三", "早于启动")   # _loop 还是 None
        self.assertEqual(adapter._queue.qsize(), 0)


class TestLifecycle(unittest.TestCase):
    def test_start_emits_messages_from_driver_thread(self) -> None:
        driver = FakeDriver()
        adapter = wechat.WeChatAdapter({}, driver=driver)
        got: list[InboundMessage] = []

        async def handler(msg: InboundMessage) -> None:
            got.append(msg)

        adapter.bind(handler)

        async def scenario() -> None:
            task = asyncio.create_task(adapter.start())
            for _ in range(50):
                if driver.on_message is not None:
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(driver.on_message, "driver 没被启动")
            # 从另一个线程回调进来，模拟 wxauto 的真实行为
            threading.Thread(target=driver.on_message, args=("张三", "你好")).start()
            for _ in range(50):
                if got:
                    break
                await asyncio.sleep(0.02)
            await adapter.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        asyncio.run(scenario())
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "你好")
        self.assertTrue(driver.started)
        self.assertTrue(driver.stopped)

    def test_driver_crash_propagates(self) -> None:
        driver = FakeDriver()
        driver.fail_with = RuntimeError("微信没登录")
        adapter = wechat.WeChatAdapter({}, driver=driver)

        async def scenario() -> None:
            with self.assertRaises(RuntimeError):
                await adapter.start()

        asyncio.run(scenario())


class TestSend(unittest.TestCase):
    def test_send_goes_through_driver(self) -> None:
        driver = FakeDriver()
        adapter = wechat.WeChatAdapter({}, driver=driver)

        async def scenario() -> SendResult:
            return await adapter.send(
                OutboundMessage(
                    platform=Platform.WECHAT,
                    kind=ChatKind.PRIVATE,
                    chat_id="张三",
                    text="收到",
                )
            )

        result = asyncio.run(scenario())
        self.assertTrue(result.ok)
        self.assertEqual(driver.sent, [("张三", "收到")])


class TestBuild(unittest.TestCase):
    def test_wxauto_mode_without_package_points_to_official(self) -> None:
        # 本机没装 wxauto 时，错误信息要把人往官方通道上引
        if wechat.WxAutoDriver.available():
            self.skipTest("本机装了 wxauto，跳过缺失路径")
        with self.assertRaises(ImportError) as ctx:
            wechat.build({"mode": "wxauto"})
        self.assertIn("official", str(ctx.exception))

    def test_available_is_bool(self) -> None:
        self.assertIsInstance(wechat.WxAutoDriver.available(), bool)


if __name__ == "__main__":
    unittest.main()
