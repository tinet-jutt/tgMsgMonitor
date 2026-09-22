from typing import Any, Dict, List


def resolve_app_version(raw_version: str, commit_sha: str) -> str:
    """归一化对外展示的应用版本。

    标签版本原样保留；main/dev 构建则附加短 Commit，避免所有构建
    都显示同一个硬编码版本号。
    """
    version = (raw_version or "").strip()
    sha = (commit_sha or "").strip()
    short_sha = "" if sha.lower() in {"dev", "unknown"} else sha[:7]

    if version and version.lower() not in {"main", "dev", "unknown"}:
        return version
    if short_sha:
        channel = "main" if version.lower() == "main" else "dev"
        return f"{channel}-{short_sha}"
    return "development"


def resolve_workflow_build_state(workflow_runs: List[Dict[str, Any]], target_sha: str) -> Dict[str, Any]:
    """归一化目标 Commit 的镜像构建状态。"""
    normalized_target = (target_sha or "").strip().lower()
    for run in workflow_runs:
        if (run.get("head_sha") or "").strip().lower() != normalized_target:
            continue
        status = (run.get("status") or "unknown").strip().lower()
        conclusion = (run.get("conclusion") or "").strip().lower()
        return {
            "image_ready": status == "completed" and conclusion == "success",
            "image_build_status": status,
            "image_build_conclusion": conclusion,
            "image_build_url": run.get("html_url") or ""
        }
    return {
        "image_ready": False,
        "image_build_status": "not_found",
        "image_build_conclusion": "",
        "image_build_url": ""
    }


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
