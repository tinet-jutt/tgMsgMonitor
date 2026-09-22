import os
import sys
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config_utils import remove_account_rule_bindings


class DeleteAccountTests(unittest.TestCase):
    def test_removes_explicit_rule_bindings(self):
        phone = "+8613800000000"
        rules = [
            {"id": "only-deleted", "accounts": [phone]},
            {"id": "mixed", "accounts": [phone, "+8613900000000"]},
            {"id": "all", "accounts": ["all"]},
            {"id": "already-empty", "accounts": []},
            {"id": "legacy-without-accounts"},
        ]

        updated_count = remove_account_rule_bindings(rules, phone)

        self.assertEqual(rules[0]["accounts"], [])
        self.assertEqual(rules[1]["accounts"], ["+8613900000000"])
        self.assertEqual(rules[2]["accounts"], ["all"])
        self.assertEqual(rules[3]["accounts"], [])
        self.assertNotIn("accounts", rules[4])
        self.assertEqual(updated_count, 2)

    def test_all_accounts_rule_is_not_changed(self):
        phone = "+8613800000000"
        rules = [{"id": "all", "accounts": ["all"]}]

        updated_count = remove_account_rule_bindings(rules, phone)

        self.assertEqual(rules[0]["accounts"], ["all"])
        self.assertEqual(updated_count, 0)


if __name__ == "__main__":
    unittest.main()
