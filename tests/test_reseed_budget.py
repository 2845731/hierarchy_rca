"""补种预算与总预算边界；这些测试需要用户允许后再运行。"""

import unittest
from dataclasses import replace

from settings import CONFIG
from state import Budget, Limit


class ReseedBudgetTest(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(
            CONFIG,
            max_model_calls=25,
            meta_reserve=4,
            max_tool_calls=8,
            reseed_model_reserve=5,
            reseed_tool_reserve=2,
        )
        self.budget = Budget(self.cfg)

    def test_model_reserve_is_available_to_supplement_and_released(self):
        for _ in range(16):
            self.budget.model("seed")
        with self.assertRaises(Limit):
            self.budget.model("seed")
        self.budget.model("seed", supplement=True)
        self.assertEqual(self.budget.reseed_models_left, 4)
        self.budget.release_reseed()
        self.budget.model("seed")
        self.assertEqual(self.budget.models, 18)

    def test_tool_reserve_keeps_total_limit(self):
        for _ in range(6):
            self.budget.tool()
        with self.assertRaises(Limit):
            self.budget.tool()
        self.budget.register_reseed("S02")
        self.budget.tool("S02-C01")
        self.budget.tool("S02-C01")
        with self.assertRaises(Limit):
            self.budget.tool("S02-C01")
        self.assertEqual(self.budget.tools, self.cfg.max_tool_calls)
