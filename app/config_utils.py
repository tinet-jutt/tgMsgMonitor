from typing import Any, Dict, List


def remove_account_rule_bindings(rules: List[Dict[str, Any]], phone: str) -> int:
    """从显式绑定指定账号的监控规则中移除该账号。

    绑定为 ["all"] 的规则表示动态使用所有账号，不需要修改。
    返回实际被更新的规则数量。
    """
    updated_rules_count = 0
    for rule in rules:
        rule_accounts = rule.get("accounts")
        if not isinstance(rule_accounts, list) or phone not in rule_accounts:
            continue
        rule["accounts"] = [account for account in rule_accounts if account != phone]
        updated_rules_count += 1
    return updated_rules_count
