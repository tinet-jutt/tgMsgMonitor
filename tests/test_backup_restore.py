import os
import sys
import json
import zipfile
import io
import shutil

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.backend import app, config_manager, parse_and_validate_backup_data

def test_backup_and_restore():
    # 模拟管理员登录获取 token
    client = TestClient(app)
    
    # 1. 登录
    login_res = client.post("/api/auth/login", json={"password": "admin"})
    assert login_res.status_code == 200, f"Login failed: {login_res.text}"
    token = login_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    print("=== [测试 1] 导出 JSON 配置备份 ===")
    res = client.get("/api/system/backup/export?type=json", headers=headers)
    assert res.status_code == 200, f"Export JSON failed: {res.text}"
    assert "application/json" in res.headers.get("content-type", "")
    assert "attachment" in res.headers.get("content-disposition", "")
    export_json = json.loads(res.content.decode("utf-8"))
    assert export_json["app"] == "tgMsgMonitor"
    assert "config" in export_json
    cfg = export_json["config"]
    assert "rules" in cfg
    assert "accounts" in cfg
    assert "global_webhook" in cfg
    print(f"JSON 导出成功，包含 {len(cfg.get('rules', []))} 条规则，{len(cfg.get('accounts', []))} 个账号。")

    print("=== [测试 2] 导出 ZIP 完整迁移包 ===")
    res_zip = client.get("/api/system/backup/export?type=zip", headers=headers)
    assert res_zip.status_code == 200, f"Export ZIP failed: {res_zip.text}"
    assert "application/zip" in res_zip.headers.get("content-type", "")
    zf = zipfile.ZipFile(io.BytesIO(res_zip.content), "r")
    namelist = zf.namelist()
    assert "config.json" in namelist, "config.json not found in ZIP"
    assert "backup_meta.json" in namelist, "backup_meta.json not found in ZIP"
    meta = json.loads(zf.read("backup_meta.json").decode("utf-8"))
    assert meta["app"] == "tgMsgMonitor"
    print(f"ZIP 导出成功，包含文件：{namelist}")

    print("=== [测试 3] 备份预览接口 (Preview) ===")
    # 3.1 预览合法 JSON
    sample_backup = {
        "app": "tgMsgMonitor",
        "exported_at": "2026/09/08 10:00:00",
        "config": {
            "admin_password": "custom_password_123",
            "server": {"host": "127.0.0.1", "port": 9999},
            "accounts": [{"phone": "+19998887777", "api_id": 12345, "api_hash": "abc", "is_active": True}],
            "rules": [
                {"id": "r1", "name": "Rule 1", "targets": ["@channel1"], "filters": {"keywords": ["test"]}},
                {"id": "r2", "name": "Rule 2", "targets": ["@channel2"], "filters": {"keywords": ["alert"]}}
            ],
            "todos": [
                {"id": "t1", "title": "Todo 1", "content": "Desc 1", "target_date": "2026/09/09 12:00"}
            ],
            "global_webhook": {"url": "https://example.com/webhook", "timeout": 10, "method": "POST"},
            "global_bark": {"is_enabled": True, "device_key": "bark_key_abc"}
        }
    }
    preview_res = client.post(
        "/api/system/backup/preview",
        headers=headers,
        data={"json_content": json.dumps(sample_backup)}
    )
    assert preview_res.status_code == 200, f"Preview failed: {preview_res.text}"
    preview_data = preview_res.json()
    assert preview_data["valid"] is True
    assert preview_data["type"] == "json"
    assert preview_data["stats"]["rules_count"] == 2
    assert preview_data["stats"]["todos_count"] == 1
    assert preview_data["stats"]["accounts_count"] == 1
    assert preview_data["stats"]["has_global_webhook"] is True
    assert preview_data["stats"]["has_global_bark"] is True
    print("JSON 预览成功，统计匹配正确。")

    # 3.2 预览 ZIP 文件
    sample_zip_buf = io.BytesIO()
    with zipfile.ZipFile(sample_zip_buf, "w") as sz:
        sz.writestr("config.json", json.dumps(sample_backup["config"]))
        sz.writestr("backup_meta.json", json.dumps({"app": "tgMsgMonitor", "exported_at": "2026/09/08 10:00"}))
        sz.writestr("sessions/session_+19998887777.session", "fake_sqlite_session_data")
    sample_zip_bytes = sample_zip_buf.getvalue()

    preview_zip_res = client.post(
        "/api/system/backup/preview",
        headers=headers,
        files={"file": ("backup.zip", sample_zip_bytes, "application/zip")}
    )
    assert preview_zip_res.status_code == 200, f"ZIP preview failed: {preview_zip_res.text}"
    p_zip_data = preview_zip_res.json()
    assert p_zip_data["valid"] is True
    assert p_zip_data["type"] == "zip"
    assert p_zip_data["stats"]["session_files_count"] == 1
    print("ZIP 预览成功，Session 文件统计匹配正确。")

    # 3.3 损坏/非法数据测试
    bad_res = client.post(
        "/api/system/backup/preview",
        headers=headers,
        data={"json_content": "invalid json {[[["}
    )
    assert bad_res.status_code == 400
    print("非法数据校验拦截正确。")

    print("=== [测试 4] 配置导入 (保留当前密码与端口) ===")
    # 记录当前密码与端口
    with open("config.json", "r", encoding="utf-8") as f:
        orig_config = json.load(f)
    orig_pwd = orig_config.get("admin_password", "admin")
    orig_port = orig_config.get("server", {}).get("port", 8010)

    import_res = client.post(
        "/api/system/backup/import",
        headers=headers,
        data={
            "json_content": json.dumps(sample_backup),
            "preserve_password": "true",
            "preserve_server": "true",
            "auto_reload": "true"
        }
    )
    assert import_res.status_code == 200, f"Import failed: {import_res.text}"
    import_data = import_res.json()
    assert import_data["status"] == "success"
    assert "snapshot_file" in import_data
    
    # 验证本地快照文件是否存在
    snapshot_path = os.path.join("backups", import_data["snapshot_file"])
    assert os.path.exists(snapshot_path), f"Snapshot file {snapshot_path} not created"
    print(f"安全快照已生成：{snapshot_path}")

    # 读取导入后的 config.json 验证保护字段
    with open("config.json", "r", encoding="utf-8") as f:
        new_config = json.load(f)
    assert new_config["admin_password"] == orig_pwd, "Admin password should be preserved"
    assert new_config["server"]["port"] == orig_port, "Server port should be preserved"
    assert len(new_config["rules"]) == 2, "Rules should be imported"
    assert len(new_config["todos"]) == 1, "Todos should be imported"
    print("配置导入成功，密码与端口保护生效，规则与待办恢复正确。")

    print("=== [测试 5] ZIP 导入与 Session 恢复 ===")
    zip_import_res = client.post(
        "/api/system/backup/import",
        headers=headers,
        files={"file": ("full_backup.zip", sample_zip_bytes, "application/zip")},
        data={"preserve_password": "true", "preserve_server": "true", "auto_reload": "false"}
    )
    assert zip_import_res.status_code == 200, f"ZIP import failed: {zip_import_res.text}"
    # 验证 sessions 目录下 session_+19998887777.session 是否被解压
    restored_session = os.path.join("sessions", "session_+19998887777.session")
    assert os.path.exists(restored_session), "Session file should be extracted to sessions directory"
    with open(restored_session, "r", encoding="utf-8") as f:
        assert f.read() == "fake_sqlite_session_data"
    print("ZIP 导入成功，Session 凭据文件安全解压恢复正确。")
    # 清理临时 session 测试文件
    if os.path.exists(restored_session):
        os.remove(restored_session)

    # 恢复原始配置
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(orig_config, f, indent=2, ensure_ascii=False)
    print("=== 全部测试通过！已恢复原始配置。===")

if __name__ == "__main__":
    test_backup_and_restore()
