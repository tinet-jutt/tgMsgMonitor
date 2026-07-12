# Telegram Message Monitor (Multi-Account & Web Console)

A robust, self-hosted, multi-account Telegram message monitoring and forwarding system. Built on **FastAPI** and **Telethon (MTProto API)** with a sleek, responsive glassmorphism dark-mode Web GUI. Automatically filters incoming messages and triggers custom Webhook endpoints.

[English](#english) | [简体中文](#简体中文)

---

## English

### ⚡ Key Features

* **Multi-Account Concurrent Listening**: Runs multiple Telegram accounts simultaneously under a shared asynchronous event loop. Persistent sessions are kept in a local folder for passwordless auto-reconnections.
* **Granular Filter Rules**: Listen to specific targets (chats, groups, channels, bots, or direct messages). Supports inclusive/exclusive keywords and **Regular Expressions**.
* **Flexible Webhook Delivery**: Supports both `GET` and `POST` methods. 
  - **GET**: Parameters are safely appended as query strings if no placeholders are found.
  - **POST**: Fully custom JSON body structures with placeholder replacement, or fallback to the full system payload if left blank.
* **Rich Placeholder Replacements**: Inject dynamic variables (e.g., `{text}`, `{sender_name}`, `{chat_title}`, `{receiver_account}`) anywhere in the Webhook URL or custom POST JSON body.
* **Instant Diagnostic Tool**: Built-in connection tester to fire mock payloads and check HTTP status code, latency, and downstream server response body in real-time.
* **Admin Access Security**: Secured by a password authorization wall (default: `admin`, changeable in console) with an anti-FOUC (Flash of Unstyled Content) loading overlay to lock dashboard contents before authentication.
* **Configurable Network Binding**: Custom Host (default: `0.0.0.0` to accept external network requests) and custom Port binding, specified directly in the configuration file.

---

### 📦 Quick Start

#### 1. Prerequisites
- Python 3.10+
- Telegram API Credentials (`api_id` and `api_hash`) for each account. Get them from the official portal: [https://my.telegram.org](https://my.telegram.org).

#### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

#### 3. Run the Server
```bash
python main.py
```
The server will boot up and load configurations. If the default port `8000` is occupied, it automatically shifts or reads the designated port in `config.json` (e.g., `8010`):
```text
************************************************************
  Telegram 消息监控系统 Web 管理后台已准备就绪。
  请使用浏览器打开: http://127.0.0.1:8010
  (已绑定网络接口 0.0.0.0:8010)
  (如需终止服务，请在终端按 Ctrl+C)
************************************************************
```

#### 4. Access the Web Console
Open **[http://127.0.0.1:8010](http://127.0.0.1:8010)** in your browser.
- **Default password**: `admin`
- Go to the top-right corner to **Change Admin Password** immediately after entry.

#### 5. Docker One-Click Deployment (Recommended)
Alternatively, you can build and run the application inside a Docker container:

* **Using Docker Compose**:
  Directly spin up the container in the background with volume persistency:
  ```bash
  docker compose up -d --build
  ```
* **Using Native Docker CLI**:
  Build the image manually and run it:
  ```bash
  docker build -t tg-monitor .
  docker run -d --name tg_msg_monitor \
    -p 8010:8010 \
    -v ./sessions:/app/sessions \
    -v ./config.json:/app/config.json \
    --restart always \
    tg-monitor
  ```
*(Note: Persisting the `./sessions` folder and `./config.json` is highly recommended so that you won't need to log in again after a container restart).*

---

### 📖 Placeholders Reference

The system supports the following placeholders which will be safely URL-encoded in GET requests:

| Placeholder | Meaning | Example Value |
| :--- | :--- | :--- |
| `{text}` | Message text content | `Looking for remote python developers` |
| `{sender_name}` | Sender's display name | `Bob Smith` |
| `{sender_username}` | Sender's TG username (without `@`) | `recruiter_bob` |
| `{chat_title}` | Group/Channel title or chat name | `Remote Work Exchange` |
| `{receiver_account}`| Phone number of the monitoring client | `+18255729287` |
| `{matched_keywords}`| Matched filtering tags (comma-separated)| `remote,python` |
| `{msg_id}` | Unique message integer ID | `14205` |
| `{chat_id}` | Chat integer ID | `-100123456789` |
| `{sender_id}` | Sender user integer ID | `987654321` |
| `{date}` | ISO datetime string | `2026-07-11T21:48:47+08:00` |

---

### 🔗 Webhook Full Payload Format

If a custom POST body is not provided, the default payload sent to your Webhook endpoint (as `application/json`) is structured as follows:

```json
{
  "rule_name": "Job Recruitment Monitor",
  "receiver_account": "+18255729287",
  "matched_keywords": ["remote", "python"],
  "message": {
    "id": 14205,
    "text": "Looking for remote python developers. Contract details...",
    "date": "2026-07-11T21:48:47+08:00"
  },
  "sender": {
    "id": 987654321,
    "username": "recruiter_bob",
    "first_name": "Bob",
    "last_name": "Smith",
    "is_bot": false
  },
  "chat": {
    "id": -100123456789,
    "title": "Remote Work Exchange",
    "type": "supergroup",
    "username": "remote_jobs_channel"
  }
}
```

---

## 简体中文

一个健壮、自托管的多账号 Telegram 消息监控与转发系统。基于 **FastAPI** 与 **Telethon (MTProto API)** 构建，并配备了精美、响应迅速的磨砂玻璃科技风 Web 管理后台。系统能够自动过滤接收到的消息并触发自定义的 Webhook 推送。

### ⚡ 核心功能

* **多账号并发监听**：基于 MTProto 协议支持同时运行多个 Telegram 账号客户端，并在同一个异步事件循环中高效分发。登录状态（Session）安全保存在本地，后续启动系统将自动免密重连。
* **细粒度过滤规则**：支持对特定监听目标（群组、频道、机器人或私聊会话）设定匹配策略，支持包含词、排除词和**正则表达式**。
* **多样化 Webhook 推送**：支持 `GET` 和 `POST` 请求类型。
  - **GET 模式**：如果未手动配置占位符，系统默认会将主要监控字段作为 QueryString 参数安全追加到 URL 尾部。
  - **POST 模式**：支持**自定义 JSON 请求体**并自动替换内部占位符；若留空则默认发送全量监控 Payload。
* **丰富占位符变量**：支持 `{text}`、`{sender_name}`、`{chat_title}`、`{receiver_account}` 等 10 余个占位符，安全处理 URL 编码与 JSON 转义。
* **连接性测试工具**：网页端内置一键测试 Webhook 连接按钮，携带 Mock 占位符数据进行真实网络请求，当场诊断响应状态码、耗时和返回包。
* **管理员安全鉴权**：内置全屏密码锁定遮罩（默认密码 `admin`，支持在后台修改），集成防闪现（Anti-FOUC）指令限制，防止未经授权非法访问控制台和 Telegram 敏感接口。
* **网络绑定与端口重定向**：支持在配置文件中指定监听 host（默认 `0.0.0.0` 允许公网或局域网访问）和端口，若配置端口冲突会自动输出日志进行警示并优雅退出。

---

### 📦 安装与运行

#### 1. 前置依赖
- Python 3.10+
- 需要绑定的 Telegram 账号 API 凭证 (`api_id` 和 `api_hash`)。申请地址：[https://my.telegram.org](https://my.telegram.org)。

#### 2. 安装项目依赖
```bash
pip install -r requirements.txt
```

#### 3. 运行服务
```bash
python main.py
```
如需自定义网络监听接口，直接在自动生成的 `config.json` 的 `server` 节点中修改 `host` 和 `port`。端口默认配置为 `8010`：
```text
************************************************************
  Telegram 消息监控系统 Web 管理后台已准备就绪。
  请使用浏览器打开: http://127.0.0.1:8010
  (已绑定网络接口 0.0.0.0:8010)
  (如需终止服务，请在终端按 Ctrl+C)
************************************************************
```

#### 4. 访问管理页面
在浏览器中打开 **[http://127.0.0.1:8010](http://127.0.0.1:8010)**。
- **默认管理员密码**：`admin`
- 进入系统后，建议第一时间点击右上角 **“安全密码”** 修改访问密码。

#### 5. Docker 一键部署 (推荐)
您也可以选择在 Docker 容器中进行快速构建与一键式部署：

* **使用 Docker Compose**:
  在项目根目录下直接在后台启动容器（已包含卷持久化配置）：
  ```bash
  docker compose up -d --build
  ```
* **使用原生 Docker 命令行**:
  手动编译镜像并创建容器实例：
  ```bash
  docker build -t tg-monitor .
  docker run -d --name tg_msg_monitor \
    -p 8010:8010 \
    -v ./sessions:/app/sessions \
    -v ./config.json:/app/config.json \
    --restart always \
    tg-monitor
  ```
*(注意：请务必挂载并持久化主机上的 `./sessions` 和 `./config.json`，确保容器在意外重启后依然能自动免密重连您的 Telegram 账号配置。)*

---

### 📖 支持的占位符

| 占位符 (点击复制) | 替换内容含义 | 示例值 |
| :--- | :--- | :--- |
| `{text}` | 消息文本内容 | `招募远程 Python 开发` |
| `{sender_name}` | 发信人昵称 (优先 First+Last Name) | `Bob Smith` |
| `{sender_username}` | 发信者 Telegram Username (不含 @) | `recruiter_bob` |
| `{chat_title}` | 消息会话名称（群组/频道标题/私聊名） | `远程工作交流群` |
| `{receiver_account}` | 接收该消息的当前监控手机号 | `+18255729287` |
| `{matched_keywords}` | 命中的过滤关键字 (以英文逗号分隔) | `远程,Python` |
| `{msg_id}` | 消息在 Telegram 中的唯一数字 ID | `14205` |
| `{chat_id}` | 消息来源会话的数字 ID | `-100123456789` |
| `{sender_id}` | 发送人的数字 ID | `987654321` |
| `{date}` | 消息时间 (符合 ISO 标准的字符串) | `2026-07-11T21:48:47+08:00` |

---

### 🗄️ 文件目录结构

```text
tgMsgMonitor/
├── app/
│   ├── static/
│   │   └── index.html      # 磨砂玻璃科技风单页面 WebGUI
│   └── backend.py          # FastAPI 服务与多账号 Telethon 监听并发管理
├── sessions/               # [自动生成] 保存已登录的 Telegram 认证 session 缓存
├── config.json             # [自动生成] 保存管理员密码、网络绑定及监听规则的配置文件
├── requirements.txt        # 第三方依赖库列表
├── main.py                 # 程序入口，加载配置参数并启动 uvicorn
└── README.md               # 项目说明文档 (双语)
```
