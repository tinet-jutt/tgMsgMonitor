import os
import sys
import json
import uvicorn
from fastapi.staticfiles import StaticFiles

# 确保当前目录在 sys.path 中，以便模块导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.backend import app

# 静态文件目录路径
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)

# 挂载静态文件
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

if __name__ == "__main__":
    # 从配置文件中读取绑定 Host 和 Port
    host = "0.0.0.0"
    port = 8000
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                server_cfg = cfg.get("server", {})
                host = server_cfg.get("host", "0.0.0.0")
                port = server_cfg.get("port", 8000)
        except Exception:
            pass

    display_host = "127.0.0.1" if host == "0.0.0.0" else host

    print("*" * 60)
    print("  Telegram 消息监控系统 Web 管理后台已准备就绪。")
    print(f"  请使用浏览器打开: http://{display_host}:{port}")
    print(f"  (已绑定网络接口 {host}:{port})")
    print("  (如需终止服务，请在终端按 Ctrl+C)")
    print("*" * 60)
    
    # 启动 Uvicorn 服务
    uvicorn.run("main:app", host=host, port=port, reload=False)
