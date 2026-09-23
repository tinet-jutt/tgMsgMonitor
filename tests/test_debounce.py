import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backend import TelegramManager


class TestDebounceLogic(unittest.TestCase):
    def setUp(self):
        self.mock_config_manager = MagicMock()
        self.manager = TelegramManager(self.mock_config_manager)
        self.rule_id = "test_rule_1"

    def test_initial_trigger_and_debounce_activation(self):
        """测试首次触发消息时开启防抖状态"""
        now = 1000.0
        debounce_seconds = 300  # 5 分钟

        # 模拟首条消息到达并开启防抖
        self.manager.rule_debounce_state[self.rule_id] = {
            "last_activity": now,
            "debounce_seconds": debounce_seconds,
            "until": now + debounce_seconds
        }
        self.manager.rule_debounce_until[self.rule_id] = now + debounce_seconds

        state = self.manager.rule_debounce_state.get(self.rule_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["until"], 1300.0)
        self.assertEqual(state["debounce_seconds"], 300)

    def test_update_debounce_shortened_within_window(self):
        """测试在冷却期内将防抖时间缩短（如 300s -> 180s），但尚未超期"""
        t0 = 1000.0
        # 初始 300 秒 (5分钟)
        self.manager.rule_debounce_state[self.rule_id] = {
            "last_activity": t0,
            "debounce_seconds": 300,
            "until": t0 + 300
        }
        self.manager.rule_debounce_until[self.rule_id] = t0 + 300

        # 模拟在 T0 + 60s 时，用户调整防抖时间为 180 秒 (3分钟)
        current_time = t0 + 60.0
        import app.backend as backend_module
        original_time = backend_module.time.time
        try:
            backend_module.time.time = lambda: current_time
            self.manager.update_rule_debounce(self.rule_id, 180)
        finally:
            backend_module.time.time = original_time

        state = self.manager.rule_debounce_state.get(self.rule_id)
        self.assertIsNotNone(state)
        # 截止时间应变为 t0 + 180 = 1180.0
        self.assertEqual(state["until"], 1180.0)
        self.assertEqual(state["debounce_seconds"], 180)
        self.assertEqual(self.manager.rule_debounce_until[self.rule_id], 1180.0)

    def test_update_debounce_expired_immediately(self):
        """测试在冷却期内将防抖时间缩短至已经过去的时间（如已过 60s，调整为 30s），应立即解除冷却"""
        t0 = 1000.0
        # 初始 300 秒 (5分钟)
        self.manager.rule_debounce_state[self.rule_id] = {
            "last_activity": t0,
            "debounce_seconds": 300,
            "until": t0 + 300
        }
        self.manager.rule_debounce_until[self.rule_id] = t0 + 300

        # 在 T0 + 60s 时，用户调整防抖时间为 30 秒 (小于已过去的 60 秒)
        current_time = t0 + 60.0
        import app.backend as backend_module
        original_time = backend_module.time.time
        try:
            backend_module.time.time = lambda: current_time
            self.manager.update_rule_debounce(self.rule_id, 30)
        finally:
            backend_module.time.time = original_time

        # 应该立即解除防抖冷却
        self.assertNotIn(self.rule_id, self.manager.rule_debounce_state)
        self.assertNotIn(self.rule_id, self.manager.rule_debounce_until)

    def test_disable_debounce_immediately_clears_state(self):
        """测试用户将防抖设置为 0（禁用防抖）时，立即清除冷却状态"""
        t0 = 1000.0
        self.manager.rule_debounce_state[self.rule_id] = {
            "last_activity": t0,
            "debounce_seconds": 300,
            "until": t0 + 300
        }
        self.manager.rule_debounce_until[self.rule_id] = t0 + 300

        self.manager.update_rule_debounce(self.rule_id, 0)

        self.assertNotIn(self.rule_id, self.manager.rule_debounce_state)
        self.assertNotIn(self.rule_id, self.manager.rule_debounce_until)

    def test_clear_rule_debounce_on_delete_or_disable(self):
        """测试删除规则或停用规则时清理防抖缓存"""
        self.manager.rule_debounce_state[self.rule_id] = {
            "last_activity": 1000.0,
            "debounce_seconds": 300,
            "until": 1300.0
        }
        self.manager.rule_debounce_until[self.rule_id] = 1300.0

        self.manager.clear_rule_debounce(self.rule_id)

        self.assertNotIn(self.rule_id, self.manager.rule_debounce_state)
        self.assertNotIn(self.rule_id, self.manager.rule_debounce_until)


if __name__ == "__main__":
    unittest.main()
