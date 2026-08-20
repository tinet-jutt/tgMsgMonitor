import os
import json
import asyncio
import re
from typing import List, Dict, Any, Optional, Tuple
from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Depends, Header, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    AuthKeyUnregisteredError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    SessionRevokedError
)
from datetime import datetime, timedelta
import calendar
from loguru import logger
import httpx
import secrets
import time

# 确保全局默认时区为 Asia/Shanghai (UTC+8)
if "TZ" not in os.environ:
    os.environ["TZ"] = "Asia/Shanghai"
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

# 基础目录配置
CONFIG_PATH = "config.json"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# 内存缓存，保存 username 到 Telegram 数字 ID 的映射
username_to_id_cache: Dict[str, int] = {}

import urllib.parse

# ----------------- 占位符解析与日期格式化工具函数 -----------------

def format_datetime(dt=None) -> str:
    """统一将 datetime 对象或当前时间格式化为 YYYY/MM/DD HH:mm (例如 2026/08/05 13:24)"""
    if dt is None:
        dt = datetime.now()
    if hasattr(dt, 'astimezone'):
        try:
            dt = dt.astimezone()
        except Exception:
            pass
    return dt.strftime("%Y/%m/%d %H:%M")

def resolve_placeholders(url: str, placeholder_data: Dict[str, Any], method: str) -> str:
    """
    使用实际的值替换 URL 中的占位符（如 {text}），并进行安全的 URL 编码。
    如果 method 为 GET 且 URL 中无任何占位符，则自动将主要参数转为 Query parameters 拼接到 URL 尾部。
    """
    def replace_match(match):
        key = match.group(1)
        val = placeholder_data.get(key, "")
        return urllib.parse.quote(str(val))

    new_url, count = re.subn(r'\{([a-zA-Z0-9_]+)\}', replace_match, url)

    if method.upper() == "GET" and count == 0:
        params = urllib.parse.urlencode({k: str(v) for k, v in placeholder_data.items()})
        separator = "&" if "?" in new_url else "?"
        new_url = f"{new_url}{separator}{params}"

    return new_url

def resolve_template_text(template: str, placeholder_data: Dict[str, Any]) -> str:
    """
    替换文本模板中的占位符（如 {text}, {title}）为原始文本（不进行 URL 编码）。
    用于 Bark 标题、正文、分组等文本字段。
    """
    if not template:
        return ""
    def replace_match(match):
        key = match.group(1)
        return str(placeholder_data.get(key, ""))
    return re.sub(r'\{([a-zA-Z0-9_]+)\}', replace_match, template)

def extract_bark_info(key_or_url: str, default_server: str = "https://api.day.app") -> Tuple[str, str]:
    """
    智能解析用户填写的 Bark 字符串：
    支持直接填入 Device Key (如 'Nxxxyyy')，
    或直接粘贴完整的 Bark 推送 URL (如 'https://api.day.app/Nxxxyyy' 或 'http://bark.myhost.com/Nxxxyyy/推送标题/正文')。
    返回 (server_url, device_key)。
    """
    raw = (key_or_url or "").strip()
    def_srv = (default_server or "https://api.day.app").strip().rstrip("/")
    if not raw:
        return def_srv, ""
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        server = f"{parsed.scheme}://{parsed.netloc}"
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        if path_parts:
            key = path_parts[0]
            return server, key
        return server, ""
    return def_srv, raw

async def send_bark_push(
    bark_config: Dict[str, Any],
    title: str,
    body: str,
    placeholder_data: Dict[str, Any],
    default_group: str = "TG监控",
    default_sound: str = "",
    default_level: str = "active"
) -> Dict[str, Any]:
    """
    发送 Bark 推送通知，并返回统一诊断结果字典
    """
    raw_key = (bark_config.get("device_key") or "").strip()
    default_srv = (bark_config.get("server_url") or "https://api.day.app").strip()
    server_url, device_key = extract_bark_info(raw_key, default_srv)

    if not device_key:
        return {"status": "error", "code": None, "elapsed": 0, "response": "", "error": "未配置 Bark Device Key"}

    timeout = bark_config.get("timeout", 10)
    group = bark_config.get("group") if bark_config.get("group") is not None else default_group
    sound = bark_config.get("sound") if bark_config.get("sound") is not None else default_sound
    level = bark_config.get("level") if bark_config.get("level") is not None else default_level
    icon = (bark_config.get("icon") or "").strip()
    click_url = (bark_config.get("url") or "").strip()
    is_archive = bark_config.get("is_archive", 1)

    # 占位符解析
    res_title = resolve_template_text(title, placeholder_data)
    res_body = resolve_template_text(body, placeholder_data)
    res_group = resolve_template_text(group, placeholder_data) if group else ""
    res_icon = resolve_template_text(icon, placeholder_data) if icon else ""
    res_click_url = resolve_template_text(click_url, placeholder_data) if click_url else ""

    endpoint = f"{server_url}/push"
    payload: Dict[str, Any] = {
        "device_key": device_key,
        "title": res_title,
        "body": res_body,
        "isArchive": is_archive
    }
    if res_group:
        payload["group"] = res_group
    if sound and sound != "default":
        payload["sound"] = sound
    if res_icon:
        payload["icon"] = res_icon
    if level and level != "active":
        payload["level"] = level
    if res_click_url:
        payload["url"] = res_click_url

    start_time = time.time()
    masked_key = f"{device_key[:4]}***{device_key[-2:]}" if len(device_key) > 6 else "***"
    logger.info(f"正在发送 Bark 推送通知 [{res_title}] 到 {endpoint} (Key: {masked_key})...")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, json=payload)
            elapsed = int((time.time() - start_time) * 1000)
            resp_text = resp.text[:1000]
            if resp.status_code == 200:
                logger.info(f"Bark 推送成功 [{res_title}], 响应: {resp_text}")
                return {"status": "success", "code": resp.status_code, "elapsed": elapsed, "response": resp_text, "error": ""}
            else:
                logger.warning(f"Bark 推送返回异常状态码 [{res_title}]: {resp.status_code}, 内容: {resp_text}")
                return {"status": "error", "code": resp.status_code, "elapsed": elapsed, "response": resp_text, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error(f"发送 Bark 推送失败 [{res_title}]: {e}")
        return {"status": "error", "code": None, "elapsed": elapsed, "response": "", "error": str(e)}



# ----------------- Pydantic 模型 -----------------

class GlobalWebhookConfig(BaseModel):
    url: str = ""
    timeout: int = 10
    method: str = "POST"  # GET / POST
    custom_body: str = ""

class OfflineWebhookConfig(BaseModel):
    url: str = ""
    timeout: int = 10
    method: str = "POST"
    custom_body: str = ""

class TodoWebhookConfig(BaseModel):
    url: str = ""
    timeout: int = 10
    method: str = "POST"
    custom_body: str = ""

class BarkConfig(BaseModel):
    is_enabled: bool = False
    server_url: str = "https://api.day.app"  # 官方服务或自建 Bark 服务
    device_key: str = ""                     # Bark Device Key 或完整 URL
    group: str = ""                          # 默认消息分组
    sound: str = ""                          # 提示音 (如 minuet, birdsong, alarm 等)
    icon: str = ""                           # 自定义通知图标 URL
    level: str = "active"                    # active / timeSensitive / passive / critical
    is_archive: int = 1                      # 1=保存历史, 0=不保存
    url: str = ""                            # 点击通知跳转的链接 (支持占位符)
    timeout: int = 10

class RuleFilter(BaseModel):
    keywords: List[str] = []
    exclude_keywords: List[str] = []
    exclude_senders: List[str] = []
    use_regex: bool = False

class RuleWebhook(BaseModel):
    url: str = ""
    method: str = ""  # 为空时继承全局
    custom_body: str = ""

class RuleBark(BaseModel):
    is_enabled: bool = False
    server_url: str = ""
    device_key: str = ""
    group: str = ""
    sound: str = ""
    icon: str = ""
    level: str = ""
    url: str = ""

class RuleModel(BaseModel):
    id: str
    name: str
    accounts: List[str] = ["all"]  # 绑定手机号列表或 ["all"]
    targets: List[str] = []         # 监听的目标，例如 ["@group", "-1001234567"]
    filters: RuleFilter
    webhook: RuleWebhook = RuleWebhook()
    bark: RuleBark = RuleBark()
    debounce_seconds: int = 0      # 防抖冷却时间（秒），0 表示禁用
    is_enabled: bool = True

class AccountModel(BaseModel):
    phone: str
    api_id: int
    api_hash: str
    session_name: str
    is_active: bool = False

class TodoWebhook(BaseModel):
    url: str = ""
    method: str = ""  # 为空时继承全局
    custom_body: str = ""

class TodoBark(BaseModel):
    is_enabled: bool = False
    server_url: str = ""
    device_key: str = ""
    group: str = ""
    sound: str = ""
    icon: str = ""
    level: str = ""
    url: str = ""

class TodoModel(BaseModel):
    id: str
    title: str
    content: str = ""
    target_date: str
    is_recurring: bool = False
    repeat_interval_value: int = 1
    repeat_interval_unit: str = "days"  # minutes, hours, days, weeks, months
    advance_value: int = 0
    advance_unit: str = "days"  # minutes, hours, days, weeks, months
    confirm_type: str = "auto"  # auto / manual
    remind_interval_minutes: int = 30
    webhook: TodoWebhook = TodoWebhook()
    bark: TodoBark = TodoBark()
    is_enabled: bool = True
    status: str = "pending"  # pending, pending_confirm, completed
    last_trigger_time: Optional[str] = None
    last_remind_time: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: Optional[str] = None

class SystemConfig(BaseModel):
    accounts: List[AccountModel] = []
    global_webhook: GlobalWebhookConfig = GlobalWebhookConfig()
    offline_webhook: OfflineWebhookConfig = OfflineWebhookConfig()
    todo_webhook: TodoWebhookConfig = TodoWebhookConfig()
    global_bark: BarkConfig = BarkConfig(group="TG监控")
    offline_bark: BarkConfig = BarkConfig(group="账号告警", sound="alarm", level="timeSensitive")
    todo_bark: BarkConfig = BarkConfig(group="定时待办", sound="alarm", level="timeSensitive")
    rules: List[RuleModel] = []
    todos: List[TodoModel] = []

# 登录 API 参数模型
class SendCodeReq(BaseModel):
    phone: str
    api_id: int
    api_hash: str

class VerifyCodeReq(BaseModel):
    phone: str
    code: str

class Verify2FAReq(BaseModel):
    phone: str
    password: str

# 登录 API 模型
class LoginReq(BaseModel):
    password: str

class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str

# ----------------- 配置管理器 -----------------

class ConfigManager:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.config = {
            "admin_password": "admin",
            "server": {"host": "0.0.0.0", "port": 8000},
            "accounts": [],
            "global_webhook": {"url": "", "timeout": 10, "method": "POST", "custom_body": ""},
            "offline_webhook": {"url": "", "timeout": 10, "method": "POST", "custom_body": ""},
            "rules": [],
            "todos": []
        }
        self.load()

    def load(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
                # 补充可能缺失的默认字段
                updated = False
                if "admin_password" not in self.config:
                    self.config["admin_password"] = "admin"
                    updated = True
                if "server" not in self.config:
                    self.config["server"] = {"host": "0.0.0.0", "port": 8000}
                    updated = True
                if "offline_webhook" not in self.config:
                    self.config["offline_webhook"] = {"url": "", "timeout": 10, "method": "POST", "custom_body": ""}
                    updated = True
                if "todos" not in self.config:
                    self.config["todos"] = []
                    updated = True
                if updated:
                    self.save_sync()
                logger.info("成功加载 config.json 配置文件。")
            except Exception as e:
                logger.error(f"加载配置文件 config.json 失败，将使用默认配置: {e}")
        else:
            self.save_sync()

    def save_sync(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            logger.info("成功保存配置到 config.json。")
        except Exception as e:
            logger.error(f"写入配置文件失败: {e}")

    async def get_config(self) -> dict:
        async with self.lock:
            return self.config

    async def save_config(self, new_config: dict):
        async with self.lock:
            self.config = new_config
            # 异步写文件
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.save_sync)

# ----------------- Telegram 客户端管理器 -----------------

class TelegramManager:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        # 活动中的客户端 {phone: TelegramClient}
        self.active_clients: Dict[str, TelegramClient] = {}
        # 登录流程中的临时客户端 {phone: TelegramClient}
        self.login_clients: Dict[str, TelegramClient] = {}
        # 登录流程中的 hash 缓存 {phone: phone_code_hash}
        self.phone_code_hashes: Dict[str, str] = {}
        # 规则防抖截止时间缓存 {rule_id: debounce_until_timestamp}
        self.rule_debounce_until: Dict[str, float] = {}

    async def init_and_start_active_accounts(self):
        """服务启动时，自动启动所有已激活的 Telegram 客户端"""
        config = await self.config_manager.get_config()
        accounts = config.get("accounts", [])
        for acc in accounts:
            phone = acc.get("phone")
            api_id = acc.get("api_id")
            api_hash = acc.get("api_hash")
            is_active = acc.get("is_active", False)

            if is_active:
                logger.info(f"正在自动启动账号监控: {phone}")
                try:
                    session_path = os.path.join(SESSIONS_DIR, f"session_{phone}")
                    client = TelegramClient(session_path, api_id, api_hash, connection_retries=None, retry_delay=5)
                    try:
                        await client.connect()
                    except Exception as conn_err:
                        logger.warning(f"账号 {phone} 初始网络连接未就绪 (将由后台自动无缝重连): {conn_err}")

                    self.active_clients[phone] = client
                    self.register_handlers(phone, client)

                    if client.is_connected():
                        if await client.is_user_authorized():
                            logger.info(f"账号 {phone} 自动连接并监听成功。")
                        else:
                            logger.warning(f"账号 {phone} 已失效或未授权，触发下线处理。")
                            await self.handle_account_offline(phone, "Session 已失效或未获得授权。")
                except Exception as e:
                    logger.error(f"初始化账号 {phone} 异常: {e}")

    async def start_keepalive_daemon(self):
        """后台保活与重连巡检协程，防网络波动断开"""
        logger.info("已启动 Telegram 客户端保活守护任务。")
        while True:
            try:
                await asyncio.sleep(30)
                config = await self.config_manager.get_config()
                accounts = config.get("accounts", [])
                for acc in accounts:
                    phone = acc.get("phone")
                    is_active = acc.get("is_active", False)
                    if not is_active:
                        continue

                    api_id = acc.get("api_id")
                    api_hash = acc.get("api_hash")
                    session_path = os.path.join(SESSIONS_DIR, f"session_{phone}")

                    if phone in self.active_clients:
                        client = self.active_clients[phone]
                        if not client.is_connected():
                            logger.warning(f"监测到账号 {phone} 连接中断，正在静默重连...")
                            try:
                                await client.connect()
                                if await client.is_user_authorized():
                                    logger.info(f"账号 {phone} 断线保活重连成功。")
                                else:
                                    logger.warning(f"账号 {phone} 重连后发现 Session 已失效。")
                                    await self.handle_account_offline(phone, "Session 已失效或在其他设备已注销。")
                            except Exception as e:
                                logger.debug(f"账号 {phone} 重连尝试中 (网络波动): {e}")
                    else:
                        logger.info(f"尝试初始化并重新拉起账号 {phone}...")
                        try:
                            client = TelegramClient(session_path, api_id, api_hash, connection_retries=None, retry_delay=5)
                            try:
                                await client.connect()
                            except Exception:
                                pass

                            self.active_clients[phone] = client
                            self.register_handlers(phone, client)

                            if client.is_connected():
                                if await client.is_user_authorized():
                                    logger.info(f"账号 {phone} 拉起并授权成功。")
                                else:
                                    logger.warning(f"账号 {phone} 已失效，触发下线。")
                                    await self.handle_account_offline(phone, "Session 已失效或未获得授权。")
                        except Exception as e:
                            logger.debug(f"拉起账号 {phone} 失败 (网络不可达): {e}")
            except Exception as e:
                logger.error(f"保活守护巡检异常: {e}")

    async def handle_account_offline(self, phone: str, reason: str = "账号已下线"):
        """统一处理真实下线（被踢/封禁/Session失效）逻辑"""
        logger.warning(f"正在执行账号下线注销流程 [{phone}]: {reason}")
        if phone in self.active_clients:
            client = self.active_clients.pop(phone)
            try:
                await client.disconnect()
            except Exception:
                pass

        config = await self.config_manager.get_config()
        updated = False
        for acc in config.get("accounts", []):
            if acc.get("phone") == phone and acc.get("is_active", False):
                acc["is_active"] = False
                updated = True
                break
        if updated:
            await self.config_manager.save_config(config)

        # 异步触发下线 Webhook 告警
        asyncio.create_task(self.trigger_offline_webhook(phone, reason))

    async def trigger_offline_webhook(self, phone: str, reason: str):
        """触发账号下线 Webhook & Bark 告警通知"""
        try:
            config = await self.config_manager.get_config()
            offline_conf = config.get("offline_webhook", {})
            url = offline_conf.get("url", "").strip()

            if not url:
                global_webhook = config.get("global_webhook", {})
                url = global_webhook.get("url", "").strip()
                timeout = global_webhook.get("timeout", 10)
                method = global_webhook.get("method", "POST")
                custom_body = global_webhook.get("custom_body", "")
            else:
                timeout = offline_conf.get("timeout", 10)
                method = offline_conf.get("method", "POST")
                custom_body = offline_conf.get("custom_body", "")

            now_str = format_datetime()
            placeholder_data = {
                "receiver_account": phone,
                "phone": phone,
                "reason": reason,
                "date": now_str,
                "event": "account_offline",
                "text": f"【账号下线告警】Telegram 账号 [{phone}] 已离线/需重新登录！原因：{reason}"
            }

            # A. 触发 Webhook 推送
            if url:
                final_url = resolve_placeholders(url, placeholder_data, method)
                logger.info(f"正在发送账号下线 Webhook 告警 [{phone}] 到 {final_url}...")
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        if method.upper() == "GET":
                            resp = await client.get(final_url)
                        else:
                            if custom_body and custom_body.strip():
                                resolved_body = resolve_placeholders(custom_body, placeholder_data, method)
                                try:
                                    payload = json.loads(resolved_body)
                                    resp = await client.post(final_url, json=payload)
                                except json.JSONDecodeError:
                                    headers = {"Content-Type": "application/json"}
                                    resp = await client.post(final_url, content=resolved_body, headers=headers)
                            else:
                                payload = {
                                    "event": "account_offline",
                                    "phone": phone,
                                    "reason": reason,
                                    "date": now_str,
                                    "message": f"Telegram 账号 [{phone}] 已离线/需重新登录！原因：{reason}"
                                }
                                resp = await client.post(final_url, json=payload)
                        logger.info(f"账号下线 Webhook 推送完成 [{phone}], 状态码: {resp.status_code}")
                except Exception as e:
                    logger.error(f"发送账号下线 Webhook 异常 [{phone}]: {e}")
            else:
                logger.info("未配置账号下线 Webhook URL，跳过 Webhook 告警推送。")

            # B. 触发 Bark 推送
            offline_bark = config.get("offline_bark", {})
            global_bark = config.get("global_bark", {})
            bark_to_use = None
            if offline_bark.get("is_enabled"):
                bark_to_use = offline_bark
                if not (bark_to_use.get("device_key") or "").strip():
                    bark_to_use = {**offline_bark, "device_key": global_bark.get("device_key", "")}
            elif global_bark.get("is_enabled") and offline_bark.get("is_enabled") is not False:
                bark_to_use = {
                    **global_bark,
                    "group": offline_bark.get("group") or "账号告警",
                    "sound": offline_bark.get("sound") or "alarm",
                    "level": offline_bark.get("level") or "timeSensitive"
                }

            if bark_to_use and (bark_to_use.get("device_key") or "").strip():
                asyncio.create_task(send_bark_push(
                    bark_config=bark_to_use,
                    title="⚠️ Telegram 账号离线告警",
                    body=f"Telegram 账号 [{phone}] 已离线/需重新登录！\n原因：{reason}",
                    placeholder_data=placeholder_data,
                    default_group="账号告警",
                    default_sound="alarm",
                    default_level="timeSensitive"
                ))
        except Exception as e:
            logger.error(f"发送账号下线 Webhook 告警失败 [{phone}]: {e}")

    def register_handlers(self, phone: str, client: TelegramClient):
        """为指定账号注册新消息监听器"""
        @client.on(events.NewMessage)
        async def handler(event):
            await self.handle_new_message(phone, event)
        logger.info(f"已为账号 {phone} 注册 NewMessage 监听器。")

    async def handle_new_message(self, phone: str, event):
        """处理监听到的新消息，执行过滤并分发 Webhook"""
        message_text = event.message.message or ""
        chat_id = event.chat_id
        logger.info(f"【监听捕获】账号 {phone} 收到事件 -> 会话ID: {chat_id} | 消息内容: {message_text[:100]}")

        config = await self.config_manager.get_config()
        rules = config.get("rules", [])
        global_webhook = config.get("global_webhook", {})

        # 异步拉取 sender 和 chat 详情，避免阻塞
        sender = None
        try:
            sender = await event.get_sender()
        except Exception as e:
            logger.debug(f"无法获取发送者信息: {e}")
        
        chat = None
        try:
            chat = await event.get_chat()
        except Exception as e:
            logger.debug(f"无法获取会话信息: {e}")

        for rule in rules:
            if not rule.get("is_enabled", True):
                continue
            
            # 1. 账号匹配
            rule_accounts = rule.get("accounts", [])
            if "all" not in rule_accounts and phone not in rule_accounts:
                continue

            # 2. 目标聊天与发送人匹配
            targets = rule.get("targets", [])
            if not await self.is_target_match(event, chat, sender, targets):
                continue

            # 3. 关键字过滤
            filters = rule.get("filters", {})
            keywords = filters.get("keywords", [])
            exclude_keywords = filters.get("exclude_keywords", [])
            use_regex = filters.get("use_regex", False)

            matched_keywords = []
            if keywords:
                is_match = False
                for kw in keywords:
                    if not kw:
                        continue
                    if use_regex:
                        try:
                            if re.search(kw, message_text, re.IGNORECASE):
                                is_match = True
                                matched_keywords.append(kw)
                        except Exception as e:
                            logger.error(f"正则表达式解析出错 [{kw}]: {e}")
                    else:
                        if kw.lower() in message_text.lower():
                            is_match = True
                            matched_keywords.append(kw)
                if not is_match:
                    continue

            # 4. 排除关键字过滤
            if exclude_keywords:
                is_excluded = False
                for ex_kw in exclude_keywords:
                    if not ex_kw:
                        continue
                    if use_regex:
                        try:
                            if re.search(ex_kw, message_text, re.IGNORECASE):
                                is_excluded = True
                                break
                        except Exception as e:
                            logger.error(f"排除正则表达式解析出错 [{ex_kw}]: {e}")
                    else:
                        if ex_kw.lower() in message_text.lower():
                            is_excluded = True
                            break
                if is_excluded:
                    logger.info(f"消息匹配到排除关键字，已拦截。内容: {message_text[:100]}...")
                    continue

            # 4.5 排除发送者过滤
            exclude_senders = filters.get("exclude_senders", [])
            if exclude_senders and sender:
                sender_username = (getattr(sender, 'username', '') or '').lower()
                sender_id = str(getattr(sender, 'id', ''))
                
                is_sender_excluded = False
                for ex_sender in exclude_senders:
                    if not ex_sender:
                        continue
                    ex_clean = str(ex_sender).strip().lstrip('@').lower()
                    if not ex_clean:
                        continue
                    
                    # 匹配 username (如 baduser 或 @baduser)
                    if sender_username and ex_clean == sender_username:
                        is_sender_excluded = True
                        break
                    # 匹配 发送者数字 ID (如 12345678)
                    if sender_id and ex_clean == sender_id:
                        is_sender_excluded = True
                        break

                if is_sender_excluded:
                    logger.info(f"消息发信人 [@{sender_username} | ID: {sender_id}] 匹配到排除发送者，已拦截。")
                    continue

            # 4.8 防抖冷却过滤 (Debounce Filter)
            rule_id = rule.get("id")
            debounce_seconds = rule.get("debounce_seconds", 0) or filters.get("debounce_seconds", 0) or 0
            if debounce_seconds > 0 and rule_id:
                now = time.time()
                debounce_until = self.rule_debounce_until.get(rule_id, 0)
                
                # 无论是否处于冷却期，符合条件的要发送消息均刷新重置该防抖时间
                self.rule_debounce_until[rule_id] = now + debounce_seconds
                
                if now < debounce_until:
                    remaining = int(debounce_until - now)
                    logger.info(f"规则 [{rule.get('name')}] 正在防抖冷却中 (剩余 {remaining} 秒)，防抖时间已重置为 {debounce_seconds} 秒，已拦截本次消息推送。")
                    continue

            # 5. 触发 Webhook
            rule_webhook = rule.get("webhook", {})
            webhook_url = rule_webhook.get("url") or global_webhook.get("url")
            webhook_timeout = global_webhook.get("timeout", 10)
            webhook_method = rule_webhook.get("method") or global_webhook.get("method") or "POST"
            webhook_custom_body = rule_webhook.get("custom_body") if rule_webhook.get("url") else global_webhook.get("custom_body") or ""

            # 整理占位符所需的数据
            message_date = format_datetime(event.message.date) if (event.message and event.message.date) else format_datetime()
            sender_username = sender.username if sender and hasattr(sender, 'username') else ""
            sender_first = sender.first_name if sender and hasattr(sender, 'first_name') else ""
            sender_last = sender.last_name if sender and hasattr(sender, 'last_name') else ""
            sender_name = f"{sender_first} {sender_last}".strip()
            if not sender_name:
                sender_name = sender_username or "Unknown"

            chat_type = "user"
            chat_title = ""
            if chat:
                from telethon.tl.types import Channel, Chat, User
                if isinstance(chat, User):
                    chat_type = "user"
                    chat_title = f"{chat.first_name or ''} {chat.last_name or ''}".strip()
                elif isinstance(chat, Channel):
                    chat_type = "channel" if chat.broadcast else "supergroup"
                    chat_title = chat.title
                elif isinstance(chat, Chat):
                    chat_type = "group"
                    chat_title = chat.title
            if not chat_title:
                chat_title = "Direct Message"

            placeholder_data = {
                "text": message_text,
                "msg_id": event.message.id,
                "date": message_date,
                "sender_id": sender.id if sender else "",
                "sender_username": sender_username,
                "sender_name": sender_name,
                "chat_id": event.chat_id,
                "chat_title": chat_title,
                "chat_username": chat.username if chat and hasattr(chat, 'username') and chat.username else "",
                "receiver_account": phone,
                "rule_name": rule.get("name", ""),
                "matched_keywords": ",".join(matched_keywords)
            }

            if webhook_url:
                # 异步运行 Webhook 任务，防止阻塞 Telethon 消息回路
                asyncio.create_task(self.trigger_webhook(
                    url=webhook_url,
                    method=webhook_method,
                    timeout=webhook_timeout,
                    rule_name=rule.get("name"),
                    matched_keywords=matched_keywords,
                    event=event,
                    sender=sender,
                    chat=chat,
                    receiver_account=phone,
                    placeholder_data=placeholder_data,
                    chat_type=chat_type,
                    custom_body=webhook_custom_body
                ))

            # 6. 异步运行 Bark 推送任务
            global_bark = config.get("global_bark", {})
            rule_bark = rule.get("bark", {})
            active_bark = None
            if rule_bark.get("is_enabled"):
                active_bark = {
                    "device_key": rule_bark.get("device_key") or global_bark.get("device_key", ""),
                    "server_url": rule_bark.get("server_url") or global_bark.get("server_url", "https://api.day.app"),
                    "group": rule_bark.get("group") or global_bark.get("group") or rule.get("name", "TG监控"),
                    "sound": rule_bark.get("sound") or global_bark.get("sound", ""),
                    "icon": rule_bark.get("icon") or global_bark.get("icon", ""),
                    "level": rule_bark.get("level") or global_bark.get("level", "active"),
                    "url": rule_bark.get("url") or global_bark.get("url", ""),
                    "timeout": global_bark.get("timeout", 10),
                    "is_archive": global_bark.get("is_archive", 1)
                }
            elif global_bark.get("is_enabled"):
                active_bark = global_bark

            if active_bark and (active_bark.get("device_key") or "").strip():
                bark_title = f"【{rule.get('name', 'TG监控')}】{chat_title} - {sender_name}"
                asyncio.create_task(send_bark_push(
                    bark_config=active_bark,
                    title=bark_title,
                    body=message_text,
                    placeholder_data=placeholder_data,
                    default_group=rule.get("name", "TG监控"),
                    default_sound="",
                    default_level="active"
                ))

    async def is_target_match(self, event, chat, sender, targets: List[str]) -> bool:
        """检查消息来源会话或发送人是否与规则中的目标之一匹配"""
        if not targets:
            return False

        chat_id = event.chat_id

        for target in targets:
            target_str = str(target).strip()
            if not target_str:
                continue

            # A. 数字 ID 匹配 (兼容 -100 开头的超级群/频道 ID 以及发送者 ID)
            clean_target = target_str[1:] if target_str.startswith('-') else target_str
            if clean_target.isdigit():
                target_int = int(target_str)
                # 匹配群组/频道/私聊 ID
                if target_int == chat_id:
                    return True
                # 兼容未包含 -100 前缀的情况
                if target_str.startswith('-100') and f"-100{chat_id}" == target_str:
                    return True
                if not target_str.startswith('-100') and f"-100{target_str}" == str(chat_id):
                    return True
                # 匹配发送人用户 ID
                if sender and hasattr(sender, 'id') and sender.id == target_int:
                    return True

            # B. 动态解析并缓存 Username 翻译为数字 ID 进行绝对匹配 (防范 Telethon 属性缓存缺失)
            username_to_check = target_str[1:] if target_str.startswith('@') else target_str
            if not username_to_check.isdigit():
                cached_id = username_to_id_cache.get(username_to_check.lower())
                if not cached_id and hasattr(event, 'client') and event.client:
                    try:
                        # 尝试异步通过 Telethon 将 username 翻译成实体并提取其唯一数字 ID
                        entity = await event.client.get_entity(username_to_check)
                        if entity:
                            cached_id = entity.id
                            username_to_id_cache[username_to_check.lower()] = cached_id
                            logger.info(f"成功将目标 username @{username_to_check} 翻译并缓存为 ID: {cached_id}")
                    except Exception as e:
                        logger.debug(f"无法为目标 @{username_to_check} 自动解析 ID: {e}")
                
                # 如果我们成功翻译了 ID，直接进行高可信度的 ID 比对
                if cached_id:
                    if chat_id == cached_id:
                        return True
                    if sender and hasattr(sender, 'id') and sender.id == cached_id:
                        return True

            # C. Username 匹配 (带或不带 @) - 会话匹配 (后备方案)
            if chat and hasattr(chat, 'username') and chat.username:
                if chat.username.lower() == username_to_check.lower():
                    return True

            # D. 发送人 (Sender) Username 匹配 (带或不带 @) - 会话匹配 (后备方案)
            if sender and hasattr(sender, 'username') and sender.username:
                if sender.username.lower() == username_to_check.lower():
                    return True

            # E. 聊天 Title 标题匹配 (模糊匹配或全匹配，这里做全匹配)
            if chat and hasattr(chat, 'title') and chat.title:
                if chat.title.lower() == target_str.lower():
                    return True

        return False

    async def trigger_webhook(
        self, url: str, method: str, timeout: int, rule_name: str, matched_keywords: List[str],
        event, sender, chat, receiver_account: str, placeholder_data: Dict[str, Any], chat_type: str,
        custom_body: str = ""
    ):
        """发送异步 Webhook 请求 (支持 GET/POST、占位符解析和自定义 Body)"""
        # 1. 对 URL 进行占位符解析与替换
        final_url = resolve_placeholders(url, placeholder_data, method)

        logger.info(f"正在以 {method} 方式发送 Webhook 消息 [{rule_name}] 到 {final_url}...")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method.upper() == "GET":
                    response = await client.get(final_url)
                else:
                    # POST 请求
                    # A. 若有自定义 Body，则先替换占位符，然后作为 JSON 或 RAW 发送
                    if custom_body and custom_body.strip():
                        resolved_body_str = resolve_placeholders(custom_body, placeholder_data, method)
                        try:
                            # 尝试解析为 JSON
                            payload = json.loads(resolved_body_str)
                            response = await client.post(final_url, json=payload)
                        except json.JSONDecodeError:
                            # 非法 JSON，以 raw text 形式发送
                            headers = {"Content-Type": "application/json"}
                            response = await client.post(final_url, content=resolved_body_str, headers=headers)
                    else:
                        # B. 发送默认的完整系统 Payload
                        message_text = event.message.message or ""
                        message_date = format_datetime(event.message.date) if (event.message and event.message.date) else format_datetime()

                        sender_info = {
                            "id": sender.id if sender else None,
                            "username": sender.username if sender and hasattr(sender, 'username') else None,
                            "first_name": sender.first_name if sender and hasattr(sender, 'first_name') else None,
                            "last_name": sender.last_name if sender and hasattr(sender, 'last_name') else None,
                            "is_bot": sender.bot if sender and hasattr(sender, 'bot') else False
                        }

                        chat_info = {
                            "id": event.chat_id,
                            "title": placeholder_data["chat_title"],
                            "type": chat_type,
                            "username": chat.username if chat and hasattr(chat, 'username') else None
                        }

                        payload = {
                            "rule_name": rule_name,
                            "receiver_account": receiver_account,
                            "matched_keywords": matched_keywords,
                            "message": {
                                "id": event.message.id,
                                "text": message_text,
                                "date": message_date
                            },
                            "sender": sender_info,
                            "chat": chat_info
                        }
                        response = await client.post(final_url, json=payload)

                if 200 <= response.status_code < 300:
                    logger.info(f"Webhook 发送成功 [{rule_name}]: 状态码 {response.status_code}")
                else:
                    logger.warning(f"Webhook 发送端返回异常代码 [{rule_name}]: 状态码 {response.status_code}")
        except Exception as e:
            logger.error(f"发送 Webhook 失败 [{rule_name}]: {e}")

    async def cleanup(self):
        """断开所有客户端连接"""
        for phone, client in list(self.active_clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
        for phone, client in list(self.login_clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
        logger.info("所有 Telegram 客户端连接已关闭。")

# ----------------- 待办日期推算与守护管理器 -----------------

def parse_target_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """解析多种常用日期时间格式为 datetime 对象"""
    if not dt_str:
        return None
    dt_str = str(dt_str).strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        pass
    return None

def advance_datetime_by_interval(dt: datetime, val: int, unit: str) -> datetime:
    """根据给定的周期数值和单位推进 datetime"""
    if val <= 0:
        val = 1
    unit = (unit or "days").lower()
    if unit in ("minute", "minutes", "m", "min"):
        return dt + timedelta(minutes=val)
    elif unit in ("hour", "hours", "h"):
        return dt + timedelta(hours=val)
    elif unit in ("day", "days", "d"):
        return dt + timedelta(days=val)
    elif unit in ("week", "weeks", "w"):
        return dt + timedelta(weeks=val)
    elif unit in ("month", "months"):
        month = dt.month - 1 + val
        year = dt.year + month // 12
        month = month % 12 + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)
    return dt + timedelta(days=val)

def calculate_advance_datetime(dt: datetime, val: int, unit: str) -> datetime:
    """根据给定的提前数值和单位计算提前触发时间 (dt - advance)"""
    if val <= 0:
        return dt
    unit = (unit or "days").lower()
    if unit in ("minute", "minutes", "m", "min"):
        return dt - timedelta(minutes=val)
    elif unit in ("hour", "hours", "h"):
        return dt - timedelta(hours=val)
    elif unit in ("day", "days", "d"):
        return dt - timedelta(days=val)
    elif unit in ("week", "weeks", "w"):
        return dt - timedelta(weeks=val)
    elif unit in ("month", "months"):
        # 减去 val 个月
        total_months = dt.year * 12 + dt.month - 1 - val
        year = total_months // 12
        month = total_months % 12 + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)
    return dt - timedelta(days=val)

def format_advance_unit_str(val: int, unit: str) -> str:
    """格式化提前提醒的中文描述"""
    unit = (unit or "days").lower()
    if unit in ("minute", "minutes", "m", "min"):
        return f"{val}分钟"
    elif unit in ("hour", "hours", "h"):
        return f"{val}小时"
    elif unit in ("day", "days", "d"):
        return f"{val}天"
    elif unit in ("week", "weeks", "w"):
        return f"{val}周"
    elif unit in ("month", "months"):
        return f"{val}个月"
    return f"{val}{unit}"

def advance_target_to_future(target_dt: datetime, val: int, unit: str, now_dt: Optional[datetime] = None) -> datetime:
    """循环推进日期，直到超过当前时间"""
    if now_dt is None:
        now_dt = datetime.now()
    cur = target_dt
    cur = advance_datetime_by_interval(cur, val, unit)
    count = 0
    while cur <= now_dt and count < 1000:
        cur = advance_datetime_by_interval(cur, val, unit)
        count += 1
    return cur

class TodoManager:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.running = False

    async def start_scheduler(self):
        self.running = True
        logger.info("已启动定时待办提醒守护任务。")
        while self.running:
            try:
                await self.check_and_trigger_todos()
            except Exception as e:
                logger.error(f"待办提醒巡检异常: {e}")
            await asyncio.sleep(10)

    async def trigger_todo_webhook(self, todo: dict, is_retry: bool = False):
        """触发待办提醒 Webhook & Bark 通知"""
        config = await self.config_manager.get_config()
        default_todo_webhook = config.get("todo_webhook", {})
        todo_custom_webhook = todo.get("webhook", {})

        url = (todo_custom_webhook.get("url") or "").strip()
        if not url:
            url = (default_todo_webhook.get("url") or "").strip()
            timeout = default_todo_webhook.get("timeout", 10)
            method = default_todo_webhook.get("method", "POST")
            custom_body = default_todo_webhook.get("custom_body", "")
        else:
            timeout = default_todo_webhook.get("timeout", 10)
            method = todo_custom_webhook.get("method") or default_todo_webhook.get("method") or "POST"
            custom_body = todo_custom_webhook.get("custom_body", "")

        now_str = format_datetime()
        title = todo.get("title", "")
        content = todo.get("content", "")
        target_date = todo.get("target_date", "")
        confirm_type_str = "手动确认" if todo.get("confirm_type") == "manual" else "自动确认"
        recurring_str = f"循环执行 (每 {todo.get('repeat_interval_value', 1)} {todo.get('repeat_interval_unit', 'days')})" if todo.get("is_recurring") else "单次执行"
        
        advance_val = todo.get("advance_value", 0) or 0
        advance_unit = todo.get("advance_unit", "days") or "days"
        has_advance = advance_val > 0
        advance_desc = f"提前{format_advance_unit_str(advance_val, advance_unit)}" if has_advance else "到期准时"

        if is_retry:
            text = f"【待办催办提醒】您的待办任务「{title}」已到期且尚未确认完成！\n到期时间：{target_date}\n任务内容：{content or '无'}"
        elif has_advance:
            text = f"【待办提前提醒】您的待办任务「{title}」即将到期（{advance_desc}提醒）！\n实际到期时间：{target_date}\n确认方式：{confirm_type_str}\n任务内容：{content or '无'}"
        else:
            text = f"【待办提醒】您的待办任务「{title}」已到期！\n到期时间：{target_date}\n确认方式：{confirm_type_str}\n任务内容：{content or '无'}"

        placeholder_data = {
            "event": "todo_reminder",
            "todo_id": todo.get("id", ""),
            "title": title,
            "content": content,
            "target_date": target_date,
            "due_date": target_date,
            "date": now_str,
            "confirm_type": confirm_type_str,
            "is_recurring": recurring_str,
            "advance_info": advance_desc,
            "status": todo.get("status", "pending"),
            "text": text
        }

        # A. 触发 Webhook 推送
        if url:
            final_url = resolve_placeholders(url, placeholder_data, method)
            logger.info(f"正在发送待办提醒 Webhook [{title}] (is_retry={is_retry}, advance={advance_desc}) 到 {final_url}...")
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    if method.upper() == "GET":
                        resp = await client.get(final_url)
                    else:
                        if custom_body and custom_body.strip():
                            resolved_body = resolve_placeholders(custom_body, placeholder_data, method)
                            try:
                                payload = json.loads(resolved_body)
                                resp = await client.post(final_url, json=payload)
                            except json.JSONDecodeError:
                                headers = {"Content-Type": "application/json"}
                                resp = await client.post(final_url, content=resolved_body, headers=headers)
                        else:
                            payload = {
                                "event": "todo_reminder",
                                "is_retry": is_retry,
                                "todo_id": todo.get("id"),
                                "title": title,
                                "content": content,
                                "target_date": target_date,
                                "advance_info": advance_desc,
                                "confirm_type": todo.get("confirm_type", "auto"),
                                "is_recurring": todo.get("is_recurring", False),
                                "trigger_time": now_str,
                                "message": text
                            }
                            resp = await client.post(final_url, json=payload)
                    logger.info(f"待办 Webhook 推送完成 [{title}], 状态码: {resp.status_code}")
            except Exception as e:
                logger.error(f"发送待办 Webhook 失败 [{title}]: {e}")
        else:
            logger.info(f"待办 [{title}] 未配置 Webhook URL，跳过 Webhook 发送。")

        # B. 触发 Bark 推送
        todo_bark_default = config.get("todo_bark", {})
        todo_bark_custom = todo.get("bark", {})
        active_bark = None
        if todo_bark_custom.get("is_enabled"):
            active_bark = {
                "device_key": todo_bark_custom.get("device_key") or todo_bark_default.get("device_key", ""),
                "server_url": todo_bark_custom.get("server_url") or todo_bark_default.get("server_url", "https://api.day.app"),
                "group": todo_bark_custom.get("group") or todo_bark_default.get("group") or "定时待办",
                "sound": todo_bark_custom.get("sound") or todo_bark_default.get("sound", "alarm"),
                "icon": todo_bark_custom.get("icon") or todo_bark_default.get("icon", ""),
                "level": todo_bark_custom.get("level") or todo_bark_default.get("level", "timeSensitive"),
                "url": todo_bark_custom.get("url") or todo_bark_default.get("url", ""),
                "timeout": todo_bark_default.get("timeout", 10),
                "is_archive": todo_bark_default.get("is_archive", 1)
            }
        elif todo_bark_default.get("is_enabled"):
            active_bark = todo_bark_default

        if active_bark and (active_bark.get("device_key") or "").strip():
            if is_retry:
                bark_title = f"🔔 待办催办：{title}"
                bark_body = f"到期时间：{target_date}\n确认方式：{confirm_type_str}\n任务内容：{content or '无'}"
            elif has_advance:
                bark_title = f"⏰ 待办提前提醒：{title}"
                bark_body = f"实际到期时间：{target_date} ({advance_desc})\n确认方式：{confirm_type_str}\n任务内容：{content or '无'}"
            else:
                bark_title = f"⏰ 待办到期：{title}"
                bark_body = f"到期时间：{target_date}\n确认方式：{confirm_type_str}\n任务内容：{content or '无'}"

            asyncio.create_task(send_bark_push(
                bark_config=active_bark,
                title=bark_title,
                body=bark_body,
                placeholder_data=placeholder_data,
                default_group="定时待办",
                default_sound="alarm",
                default_level="timeSensitive"
            ))

    async def check_and_trigger_todos(self):
        config = await self.config_manager.get_config()
        todos = config.get("todos", [])
        if not todos:
            return

        now = datetime.now()
        now_str = format_datetime(now)
        updated = False

        for todo in todos:
            if not todo.get("is_enabled", True):
                continue

            status = todo.get("status", "pending")
            if status == "completed":
                continue

            target_str = todo.get("target_date", "")
            target_dt = parse_target_datetime(target_str)
            if not target_dt:
                continue

            confirm_type = todo.get("confirm_type", "auto")
            is_recurring = todo.get("is_recurring", False)

            # 1. 处于 pending 状态，检查是否到达触发时间 (支持提前提醒)
            if status == "pending":
                advance_val = todo.get("advance_value", 0) or 0
                advance_unit = todo.get("advance_unit", "days") or "days"
                trigger_dt = calculate_advance_datetime(target_dt, advance_val, advance_unit)

                if now >= trigger_dt:
                    adv_log = f"(提前 {advance_val} {advance_unit})" if advance_val > 0 else ""
                    logger.info(f"待办任务「{todo.get('title')}」达到提醒时间 {adv_log}，开始触发提醒。")
                    asyncio.create_task(self.trigger_todo_webhook(todo, is_retry=False))
                    todo["last_trigger_time"] = now_str

                    if confirm_type == "auto":
                        if is_recurring:
                            next_dt = advance_target_to_future(
                                target_dt,
                                todo.get("repeat_interval_value", 1),
                                todo.get("repeat_interval_unit", "days"),
                                now
                            )
                            todo["target_date"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
                            todo["status"] = "pending"
                            todo["completed_at"] = now_str
                        else:
                            todo["status"] = "completed"
                            todo["completed_at"] = now_str
                    else:
                        # 手动确认模式：进入待确认催办状态
                        todo["status"] = "pending_confirm"
                        todo["last_remind_time"] = now_str
                    updated = True

            # 2. 处于 pending_confirm 状态（手动确认模式下催办）
            elif status == "pending_confirm":
                remind_interval_mins = todo.get("remind_interval_minutes", 30) or 30
                last_remind_str = todo.get("last_remind_time") or todo.get("last_trigger_time")
                last_remind_dt = parse_target_datetime(last_remind_str) if last_remind_str else None

                should_remind = False
                if not last_remind_dt:
                    should_remind = True
                else:
                    elapsed_seconds = (now - last_remind_dt).total_seconds()
                    if elapsed_seconds >= remind_interval_mins * 60:
                        should_remind = True

                if should_remind:
                    logger.info(f"待办任务「{todo.get('title')}」尚未手动确认完成，按间隔({remind_interval_mins}分钟)发送催办 Webhook...")
                    asyncio.create_task(self.trigger_todo_webhook(todo, is_retry=True))
                    todo["last_remind_time"] = now_str
                    updated = True

        if updated:
            await self.config_manager.save_config(config)

# ----------------- FastAPI 初始化与路由 -----------------

app = FastAPI(title="Telegram Message Monitor Admin API")

config_manager = ConfigManager()
tg_manager = TelegramManager(config_manager)
todo_manager = TodoManager(config_manager)

# 内存令牌缓存
current_token: Optional[str] = None

# 防爆破登录限制配置与内存记录
login_attempts: Dict[str, Dict[str, Any]] = {}
MAX_ATTEMPTS = 5
LOCKOUT_DURATION = 1800  # 30分钟

async def verify_token(authorization: Optional[str] = Header(None)):
    global current_token
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未授权，请提供登录令牌。")
    token = authorization.split("Bearer ")[1].strip()
    if not current_token or token != current_token:
        raise HTTPException(status_code=401, detail="令牌无效或已过期，请重新登录。")

# --- 鉴权管理 API ---

@app.post("/api/auth/login")
async def admin_login(req: LoginReq, request: Request):
    global current_token
    client_ip = request.client.host if request.client else "unknown"
    
    # 检查是否被锁定
    now = time.time()
    attempt = login_attempts.get(client_ip)
    if attempt and attempt.get("lock_until", 0) > now:
        remaining = int(attempt["lock_until"] - now)
        raise HTTPException(
            status_code=429,
            detail=f"尝试次数过多。该IP已被临时锁定，请在 {remaining} 秒后再试。"
        )
        
    config = await config_manager.get_config()
    saved_password = config.get("admin_password", "admin")
    
    if req.password == saved_password:
        # 验证成功，清除失败记录
        if client_ip in login_attempts:
            login_attempts.pop(client_ip, None)
        current_token = secrets.token_hex(24)
        return {"status": "success", "token": current_token}
    else:
        # 密码错误，更新记录
        if not attempt:
            attempt = {"count": 0, "lock_until": 0}
            login_attempts[client_ip] = attempt
            
        attempt["count"] += 1
        remaining_attempts = MAX_ATTEMPTS - attempt["count"]
        
        if attempt["count"] >= MAX_ATTEMPTS:
            attempt["lock_until"] = now + LOCKOUT_DURATION
            attempt["count"] = 0  # 锁定后重置计数
            raise HTTPException(
                status_code=429,
                detail="管理员密码错误。尝试次数过多，该IP已被临时锁定 30 分钟。"
            )
        else:
            # 引入 1 秒延迟惩罚，防止快速爆破
            await asyncio.sleep(1)
            raise HTTPException(
                status_code=400,
                detail=f"管理员密码错误。您还剩 {remaining_attempts} 次尝试机会。"
            )

@app.post("/api/auth/change-password", dependencies=[Depends(verify_token)])
async def change_password(req: ChangePasswordReq):
    global current_token
    config = await config_manager.get_config()
    saved_password = config.get("admin_password", "admin")
    
    if req.old_password != saved_password:
        raise HTTPException(status_code=400, detail="旧密码错误。")
        
    new_pwd = req.new_password.strip()
    if not new_pwd:
        raise HTTPException(status_code=400, detail="新密码不能为空。")
        
    config["admin_password"] = new_pwd
    await config_manager.save_config(config)
    
    # 强制使当前令牌失效，要求重新登录
    current_token = None
    return {"status": "success", "message": "密码修改成功，请重新登录。"}

@app.on_event("startup")
async def startup_event():
    # 异步初始化并自动登录已有 Session 的账号
    asyncio.create_task(tg_manager.init_and_start_active_accounts())
    # 启动后台保活重连守护
    asyncio.create_task(tg_manager.start_keepalive_daemon())
    # 启动待办提醒守护任务
    asyncio.create_task(todo_manager.start_scheduler())

@app.on_event("shutdown")
async def shutdown_event():
    await tg_manager.cleanup()
    todo_manager.running = False

# --- 账号管理 API ---

@app.get("/api/accounts", dependencies=[Depends(verify_token)])
async def get_accounts():
    config = await config_manager.get_config()
    accounts = config.get("accounts", [])
    
    result = []
    for acc in accounts:
        phone = acc["phone"]
        is_active = acc.get("is_active", False)
        
        status = "offline"
        if phone in tg_manager.login_clients:
            status = "logging_in"
        elif is_active:
            if phone in tg_manager.active_clients:
                client = tg_manager.active_clients[phone]
                if client.is_connected():
                    status = "online"
                else:
                    status = "connecting"
            else:
                status = "connecting"
        else:
            status = "offline"
            
        result.append({
            "phone": phone,
            "api_id": acc["api_id"],
            "status": status,
            "is_active": is_active
        })
    return result

@app.post("/api/auth/send-code", dependencies=[Depends(verify_token)])
async def send_code(req: SendCodeReq):
    phone = req.phone.strip()
    api_id = req.api_id
    api_hash = req.api_hash.strip()

    if not phone or not api_id or not api_hash:
        raise HTTPException(status_code=400, detail="参数不完整。")

    # 1. 如果已在线，先断开并清理
    if phone in tg_manager.active_clients:
        try:
            await tg_manager.active_clients[phone].disconnect()
        except Exception:
            pass
        del tg_manager.active_clients[phone]

    # 2. 如果存在登录中的临时 Client，先断开
    if phone in tg_manager.login_clients:
        try:
            await tg_manager.login_clients[phone].disconnect()
        except Exception:
            pass
        del tg_manager.login_clients[phone]

    # 3. 创建新客户端实例并连接
    try:
        session_path = os.path.join(SESSIONS_DIR, f"session_{phone}")
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        
        # 发送验证码
        sent_code = await client.send_code_request(phone)
        
        # 暂存到管理器中
        tg_manager.login_clients[phone] = client
        tg_manager.phone_code_hashes[phone] = sent_code.phone_code_hash
        
        # 更新 config 中的账号基本配置（暂时为未激活）
        config = await config_manager.get_config()
        # 查找是否已存在
        exist_acc = next((a for a in config["accounts"] if a["phone"] == phone), None)
        if exist_acc:
            exist_acc["api_id"] = api_id
            exist_acc["api_hash"] = api_hash
            exist_acc["is_active"] = False
        else:
            config["accounts"].append({
                "phone": phone,
                "api_id": api_id,
                "api_hash": api_hash,
                "session_name": f"session_{phone}",
                "is_active": False
            })
        await config_manager.save_config(config)

        return {"status": "need_code", "message": "验证码已发往您的 Telegram。"}
    except Exception as e:
        logger.error(f"发送验证码失败: {e}")
        raise HTTPException(status_code=500, detail=f"发送验证码错误: {str(e)}")

@app.post("/api/auth/verify-code", dependencies=[Depends(verify_token)])
async def verify_code(req: VerifyCodeReq):
    phone = req.phone.strip()
    code = req.code.strip()

    if phone not in tg_manager.login_clients:
        raise HTTPException(status_code=400, detail="登录会话未启动，请先请求发送验证码。")

    client = tg_manager.login_clients[phone]
    phone_code_hash = tg_manager.phone_code_hashes.get(phone)

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        
        # 登录成功，激活账号并运行监听
        config = await config_manager.get_config()
        for a in config["accounts"]:
            if a["phone"] == phone:
                a["is_active"] = True
                break
        await config_manager.save_config(config)

        # 转移至活动池
        tg_manager.active_clients[phone] = client
        tg_manager.register_handlers(phone, client)
        
        # 清理登录字典
        del tg_manager.login_clients[phone]
        if phone in tg_manager.phone_code_hashes:
            del tg_manager.phone_code_hashes[phone]

        return {"status": "success", "message": "登录成功！监控已启动。"}

    except SessionPasswordNeededError:
        # 需要两步验证密码
        return {"status": "need_password", "message": "账号启用了两步验证，需要输入密码。"}
    except Exception as e:
        logger.error(f"验证验证码失败: {e}")
        raise HTTPException(status_code=400, detail=f"验证码错误或过期: {str(e)}")

@app.post("/api/auth/verify-2fa", dependencies=[Depends(verify_token)])
async def verify_2fa(req: Verify2FAReq):
    phone = req.phone.strip()
    password = req.password.strip()

    if phone not in tg_manager.login_clients:
        raise HTTPException(status_code=400, detail="登录会话未启动，请重新请求。")

    client = tg_manager.login_clients[phone]

    try:
        await client.sign_in(password=password)
        
        # 登录成功，激活账号并运行监听
        config = await config_manager.get_config()
        for a in config["accounts"]:
            if a["phone"] == phone:
                a["is_active"] = True
                break
        await config_manager.save_config(config)

        # 转移至活动池
        tg_manager.active_clients[phone] = client
        tg_manager.register_handlers(phone, client)
        
        # 清理临时存储
        del tg_manager.login_clients[phone]
        if phone in tg_manager.phone_code_hashes:
            del tg_manager.phone_code_hashes[phone]

        return {"status": "success", "message": "两步验证成功，登录成功！"}
    except Exception as e:
        logger.error(f"两步验证失败: {e}")
        raise HTTPException(status_code=400, detail=f"两步验证密码错误: {str(e)}")

@app.delete("/api/accounts/{phone}", dependencies=[Depends(verify_token)])
async def delete_account(phone: str):
    phone = phone.strip()
    config = await config_manager.get_config()
    
    # 1. 从配置中移除
    new_accounts = [a for a in config["accounts"] if a["phone"] != phone]
    if len(new_accounts) == len(config["accounts"]):
        raise HTTPException(status_code=404, detail="未找到该账号配置。")
    config["accounts"] = new_accounts
    await config_manager.save_config(config)

    # 2. 从活动池中移除并断开
    if phone in tg_manager.active_clients:
        try:
            await tg_manager.active_clients[phone].disconnect()
        except Exception:
            pass
        del tg_manager.active_clients[phone]

    # 3. 从临时登录池中移除
    if phone in tg_manager.login_clients:
        try:
            await tg_manager.login_clients[phone].disconnect()
        except Exception:
            pass
        del tg_manager.login_clients[phone]
    if phone in tg_manager.phone_code_hashes:
        del tg_manager.phone_code_hashes[phone]

    # 4. 删除本地 session 文件
    session_file = os.path.join(SESSIONS_DIR, f"session_{phone}.session")
    if os.path.exists(session_file):
        try:
            os.remove(session_file)
            logger.info(f"成功清理 Session 文件: {session_file}")
        except Exception as e:
            logger.warning(f"清除 Session 文件失败: {e}")

    return {"status": "success", "message": "账号已删除并清理。"}

# --- 全局 Webhook 配置 API ---

@app.get("/api/config/webhook", dependencies=[Depends(verify_token)])
async def get_webhook_config():
    config = await config_manager.get_config()
    return config.get("global_webhook", {"url": "", "timeout": 10, "method": "POST", "custom_body": ""})

@app.post("/api/config/webhook", dependencies=[Depends(verify_token)])
async def update_webhook_config(webhook_conf: GlobalWebhookConfig):
    config = await config_manager.get_config()
    config["global_webhook"] = {
        "url": webhook_conf.url.strip(),
        "timeout": webhook_conf.timeout,
        "method": webhook_conf.method,
        "custom_body": webhook_conf.custom_body
    }
    await config_manager.save_config(config)
    return {"status": "success", "message": "全局 Webhook 配置已更新。"}

# --- 账号下线 Webhook 配置 API ---

@app.get("/api/config/offline-webhook", dependencies=[Depends(verify_token)])
async def get_offline_webhook_config():
    config = await config_manager.get_config()
    return config.get("offline_webhook", {"url": "", "timeout": 10, "method": "POST", "custom_body": ""})

@app.post("/api/config/offline-webhook", dependencies=[Depends(verify_token)])
async def update_offline_webhook_config(webhook_conf: OfflineWebhookConfig):
    config = await config_manager.get_config()
    config["offline_webhook"] = {
        "url": webhook_conf.url.strip(),
        "timeout": webhook_conf.timeout,
        "method": webhook_conf.method,
        "custom_body": webhook_conf.custom_body
    }
    await config_manager.save_config(config)
    return {"status": "success", "message": "账号下线 Webhook 配置已更新。"}

# --- 定时待办全局 Webhook 配置 API ---

@app.get("/api/config/todo-webhook", dependencies=[Depends(verify_token)])
async def get_todo_webhook_config():
    config = await config_manager.get_config()
    return config.get("todo_webhook", {"url": "", "timeout": 10, "method": "POST", "custom_body": ""})

@app.post("/api/config/todo-webhook", dependencies=[Depends(verify_token)])
async def update_todo_webhook_config(webhook_conf: TodoWebhookConfig):
    config = await config_manager.get_config()
    config["todo_webhook"] = {
        "url": webhook_conf.url.strip(),
        "timeout": webhook_conf.timeout,
        "method": webhook_conf.method,
        "custom_body": webhook_conf.custom_body
    }
    await config_manager.save_config(config)
    return {"status": "success", "message": "定时待办全局 Webhook 配置已更新。"}

# --- 全局 Bark 配置 API ---

@app.get("/api/config/global-bark", dependencies=[Depends(verify_token)])
async def get_global_bark_config():
    config = await config_manager.get_config()
    return config.get("global_bark", {
        "is_enabled": False,
        "server_url": "https://api.day.app",
        "device_key": "",
        "group": "TG监控",
        "sound": "",
        "icon": "",
        "level": "active",
        "is_archive": 1,
        "url": "",
        "timeout": 10
    })

@app.post("/api/config/global-bark", dependencies=[Depends(verify_token)])
async def update_global_bark_config(bark_conf: BarkConfig):
    config = await config_manager.get_config()
    config["global_bark"] = bark_conf.dict()
    await config_manager.save_config(config)
    return {"status": "success", "message": "全局 Bark 推送配置已更新。"}

# --- 账号下线 Bark 配置 API ---

@app.get("/api/config/offline-bark", dependencies=[Depends(verify_token)])
async def get_offline_bark_config():
    config = await config_manager.get_config()
    return config.get("offline_bark", {
        "is_enabled": False,
        "server_url": "https://api.day.app",
        "device_key": "",
        "group": "账号告警",
        "sound": "alarm",
        "icon": "",
        "level": "timeSensitive",
        "is_archive": 1,
        "url": "",
        "timeout": 10
    })

@app.post("/api/config/offline-bark", dependencies=[Depends(verify_token)])
async def update_offline_bark_config(bark_conf: BarkConfig):
    config = await config_manager.get_config()
    config["offline_bark"] = bark_conf.dict()
    await config_manager.save_config(config)
    return {"status": "success", "message": "账号下线 Bark 告警配置已更新。"}

# --- 定时待办 Bark 配置 API ---

@app.get("/api/config/todo-bark", dependencies=[Depends(verify_token)])
async def get_todo_bark_config():
    config = await config_manager.get_config()
    return config.get("todo_bark", {
        "is_enabled": False,
        "server_url": "https://api.day.app",
        "device_key": "",
        "group": "定时待办",
        "sound": "alarm",
        "icon": "",
        "level": "timeSensitive",
        "is_archive": 1,
        "url": "",
        "timeout": 10
    })

@app.post("/api/config/todo-bark", dependencies=[Depends(verify_token)])
async def update_todo_bark_config(bark_conf: BarkConfig):
    config = await config_manager.get_config()
    config["todo_bark"] = bark_conf.dict()
    await config_manager.save_config(config)
    return {"status": "success", "message": "定时待办默认 Bark 推送配置已更新。"}

# --- Webhook 测试联调 API ---

class TestWebhookReq(BaseModel):
    url: str
    method: str = "POST"
    custom_body: str = ""
    event_type: str = "message"  # "message" 或 "account_offline"

@app.post("/api/webhook/test", dependencies=[Depends(verify_token)])
async def test_webhook(req: TestWebhookReq):
    url = req.url.strip()
    method = req.method.strip().upper()
    custom_body = req.custom_body
    event_type = req.event_type

    if not url:
        raise HTTPException(status_code=400, detail="Webhook URL 不能为空。")

    if event_type == "account_offline":
        now_str = format_datetime()
        mock_placeholders = {
            "event": "account_offline",
            "receiver_account": "+15407800413",
            "phone": "+15407800413",
            "reason": "Session 已失效或在其他设备已注销 (联调测试)",
            "date": now_str,
            "text": "【账号下线告警】Telegram 账号 [+15407800413] 已离线/需重新登录！原因：Session 已失效或在其他设备已注销 (联调测试)"
        }
    elif event_type == "todo_reminder":
        now_str = format_datetime()
        mock_placeholders = {
            "event": "todo_reminder",
            "todo_id": "mock_todo_test",
            "title": "测试待办任务事项",
            "content": "这是一条来自待办 Webhook 测试按钮的模拟测试内容。",
            "target_date": now_str,
            "due_date": now_str,
            "date": now_str,
            "confirm_type": "手动确认",
            "is_recurring": "单次执行",
            "status": "pending",
            "text": f"【待办提醒】您的待办任务「测试待办任务事项」已到期！\n到期时间：{now_str}\n确认方式：手动确认\n任务内容：这是一条来自待办 Webhook 测试按钮的模拟测试内容。"
        }
    else:
        mock_placeholders = {
            "text": "这是一条来自测试按钮的测试消息内容。",
            "msg_id": 99999,
            "date": "2026/08/05 13:24",
            "sender_id": 12345678,
            "sender_username": "test_sender",
            "sender_name": "测试发送人",
            "chat_id": -100123456,
            "chat_title": "测试监控群组",
            "chat_username": "test_group",
            "receiver_account": "+8613800000000",
            "phone": "+8613800000000",
            "rule_name": "测试规则",
            "matched_keywords": "测试,监控"
        }

    final_url = resolve_placeholders(url, mock_placeholders, method)

    import time
    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if method == "GET":
                response = await client.get(final_url)
            else:
                if custom_body and custom_body.strip():
                    resolved_body_str = resolve_placeholders(custom_body, mock_placeholders, method)
                    try:
                        payload = json.loads(resolved_body_str)
                        response = await client.post(final_url, json=payload)
                    except json.JSONDecodeError:
                        headers = {"Content-Type": "application/json"}
                        response = await client.post(final_url, content=resolved_body_str, headers=headers)
                else:
                    if event_type == "account_offline":
                        payload = {
                            "event": "account_offline",
                            "phone": mock_placeholders["phone"],
                            "reason": mock_placeholders["reason"],
                            "date": mock_placeholders["date"],
                            "message": mock_placeholders["text"]
                        }
                    elif event_type == "todo_reminder":
                        payload = {
                            "event": "todo_reminder",
                            "is_retry": False,
                            "todo_id": "mock_todo_test",
                            "title": mock_placeholders["title"],
                            "content": mock_placeholders["content"],
                            "target_date": mock_placeholders["target_date"],
                            "confirm_type": "manual",
                            "is_recurring": False,
                            "trigger_time": mock_placeholders["date"],
                            "message": mock_placeholders["text"]
                        }
                    else:
                        payload = {
                            "rule_name": mock_placeholders["rule_name"],
                            "receiver_account": mock_placeholders["receiver_account"],
                            "matched_keywords": ["测试", "监控"],
                            "message": {
                                "id": mock_placeholders["msg_id"],
                                "text": mock_placeholders["text"],
                                "date": mock_placeholders["date"]
                            },
                            "sender": {
                                "id": mock_placeholders["sender_id"],
                                "username": mock_placeholders["sender_username"],
                                "first_name": "测试",
                                "last_name": "发送人",
                                "is_bot": False
                            },
                            "chat": {
                                "id": mock_placeholders["chat_id"],
                                "title": mock_placeholders["chat_title"],
                                "type": "supergroup",
                                "username": mock_placeholders["chat_username"]
                            }
                        }
                    response = await client.post(final_url, json=payload)

            elapsed = round((time.time() - start_time) * 1000, 2)
            return {
                "status": "success",
                "status_code": response.status_code,
                "elapsed_ms": elapsed,
                "response": response.text[:1000]
            }
    except Exception as e:
        elapsed = round((time.time() - start_time) * 1000, 2)
        return {
            "status": "error",
            "elapsed_ms": elapsed,
            "error": str(e)
        }

# --- Bark 测试联调 API ---

class TestBarkReq(BaseModel):
    device_key: str
    server_url: str = "https://api.day.app"
    group: str = ""
    sound: str = ""
    icon: str = ""
    level: str = "active"
    url: str = ""
    event_type: str = "message"  # "message" / "account_offline" / "todo_reminder"

@app.post("/api/bark/test", dependencies=[Depends(verify_token)])
async def test_bark(req: TestBarkReq):
    raw_key = req.device_key.strip()
    srv_url = req.server_url.strip() or "https://api.day.app"
    server_url, device_key = extract_bark_info(raw_key, srv_url)

    if not device_key:
        raise HTTPException(status_code=400, detail="Bark Device Key 不能为空。")

    now_str = format_datetime()
    if req.event_type == "account_offline":
        mock_placeholders = {
            "event": "account_offline",
            "receiver_account": "+15407800413",
            "phone": "+15407800413",
            "reason": "Session 已失效或在其他设备已注销 (Bark联调测试)",
            "date": now_str,
            "text": "【账号下线告警】Telegram 账号 [+15407800413] 已离线/需重新登录！原因：Session 已失效 (Bark联调测试)"
        }
        title = "⚠️ Telegram 账号离线告警"
        body = "Telegram 账号 [+15407800413] 已离线/需重新登录！\n原因：Session 已失效 (Bark联调测试)"
        default_group = "账号告警"
        default_sound = "alarm"
        default_level = "timeSensitive"
    elif req.event_type == "todo_reminder":
        mock_placeholders = {
            "event": "todo_reminder",
            "todo_id": "mock_todo_test",
            "title": "测试待办任务事项",
            "content": "这是一条来自待办 Bark 测试按钮的模拟测试内容。",
            "target_date": now_str,
            "due_date": now_str,
            "date": now_str,
            "confirm_type": "手动确认",
            "is_recurring": "单次执行",
            "status": "pending",
            "text": f"【待办提醒】您的待办任务「测试待办任务事项」已到期！\n到期时间：{now_str}\n确认方式：手动确认\n任务内容：这是一条来自待办 Bark 测试按钮的模拟测试内容。"
        }
        title = "⏰ 待办到期：测试待办任务事项"
        body = f"到期时间：{now_str}\n确认方式：手动确认\n任务内容：这是一条来自待办 Bark 测试按钮的模拟测试内容。"
        default_group = "定时待办"
        default_sound = "alarm"
        default_level = "timeSensitive"
    else:
        mock_placeholders = {
            "text": "这是一条来自 Bark 测试按钮的模拟 Telegram 消息内容。",
            "msg_id": 99999,
            "date": now_str,
            "sender_id": 12345678,
            "sender_username": "test_sender",
            "sender_name": "测试发送人",
            "chat_id": -100123456,
            "chat_title": "测试监控群组",
            "chat_username": "test_group",
            "receiver_account": "+8613800000000",
            "phone": "+8613800000000",
            "rule_name": "测试规则",
            "matched_keywords": "测试,Bark"
        }
        title = "【TG监控】测试监控群组 - 测试发送人"
        body = "这是一条来自 Bark 测试按钮的模拟 Telegram 消息内容。"
        default_group = "TG监控"
        default_sound = ""
        default_level = "active"

    bark_conf = {
        "device_key": device_key,
        "server_url": server_url,
        "group": req.group or default_group,
        "sound": req.sound or default_sound,
        "icon": req.icon,
        "level": req.level or default_level,
        "url": req.url,
        "timeout": 10
    }

    result = await send_bark_push(
        bark_config=bark_conf,
        title=title,
        body=body,
        placeholder_data=mock_placeholders,
        default_group=default_group,
        default_sound=default_sound,
        default_level=default_level
    )

    if result["status"] == "success":
        return {
            "status": "success",
            "status_code": result["code"],
            "elapsed_ms": result["elapsed"],
            "response": result["response"],
            "message": "Bark 推送请求已成功发送并被服务器受理。"
        }
    else:
        return {
            "status": "error",
            "status_code": result["code"],
            "elapsed_ms": result["elapsed"],
            "response": result["response"],
            "error": result["error"],
            "message": f"Bark 推送失败: {result['error']}"
        }

# --- 规则管理 API ---

@app.get("/api/rules", dependencies=[Depends(verify_token)])
async def get_rules():
    config = await config_manager.get_config()
    return config.get("rules", [])

@app.post("/api/rules", dependencies=[Depends(verify_token)])
async def create_rule(rule: RuleModel):
    config = await config_manager.get_config()
    # 校验是否已存在相同 ID
    if any(r["id"] == rule.id for r in config["rules"]):
        raise HTTPException(status_code=400, detail="规则 ID 已存在。")
    
    config["rules"].append(rule.dict())
    await config_manager.save_config(config)
    return {"status": "success", "message": "规则添加成功！"}

@app.put("/api/rules/{rule_id}", dependencies=[Depends(verify_token)])
async def update_rule(rule_id: str, updated_rule: RuleModel):
    config = await config_manager.get_config()
    rule_id = rule_id.strip()
    
    index = -1
    for i, r in enumerate(config["rules"]):
        if r["id"] == rule_id:
            index = i
            break
            
    if index == -1:
        raise HTTPException(status_code=404, detail="未找到该规则。")
        
    config["rules"][index] = updated_rule.dict()
    await config_manager.save_config(config)
    return {"status": "success", "message": "规则更新成功！"}

@app.delete("/api/rules/{rule_id}", dependencies=[Depends(verify_token)])
async def delete_rule(rule_id: str):
    config = await config_manager.get_config()
    rule_id = rule_id.strip()
    
    new_rules = [r for r in config["rules"] if r["id"] != rule_id]
    if len(new_rules) == len(config["rules"]):
        raise HTTPException(status_code=404, detail="未找到该规则。")
        
    config["rules"] = new_rules
    await config_manager.save_config(config)
    return {"status": "success", "message": "规则删除成功！"}

@app.post("/api/rules/{rule_id}/toggle", dependencies=[Depends(verify_token)])
async def toggle_rule(rule_id: str):
    config = await config_manager.get_config()
    rule_id = rule_id.strip()
    
    rule = next((r for r in config["rules"] if r["id"] == rule_id), None)
    if not rule:
        raise HTTPException(status_code=404, detail="未找到该规则。")
        
    rule["is_enabled"] = not rule.get("is_enabled", True)
    await config_manager.save_config(config)
    status_str = "启用" if rule["is_enabled"] else "暂停"
    return {"status": "success", "message": f"规则已{status_str}。"}

# --- 定时待办提醒 API ---

@app.get("/api/todos", dependencies=[Depends(verify_token)])
async def get_todos():
    config = await config_manager.get_config()
    todos = config.get("todos", [])
    now = datetime.now()

    result = []
    for t in todos:
        item = dict(t)
        target_str = t.get("target_date", "")
        target_dt = parse_target_datetime(target_str)
        if target_dt:
            diff_sec = (target_dt - now).total_seconds()
            item["remaining_seconds"] = int(diff_sec)
            item["is_overdue"] = diff_sec < 0
        else:
            item["remaining_seconds"] = 0
            item["is_overdue"] = False
        result.append(item)
    return result

@app.post("/api/todos", dependencies=[Depends(verify_token)])
async def create_todo(todo: TodoModel):
    config = await config_manager.get_config()
    if "todos" not in config:
        config["todos"] = []

    if any(t["id"] == todo.id for t in config["todos"]):
        raise HTTPException(status_code=400, detail="待办 ID 已存在。")

    todo_dict = todo.dict()
    if not todo_dict.get("created_at"):
        todo_dict["created_at"] = format_datetime()

    config["todos"].append(todo_dict)
    await config_manager.save_config(config)
    return {"status": "success", "message": "待办任务创建成功！", "todo": todo_dict}

@app.put("/api/todos/{todo_id}", dependencies=[Depends(verify_token)])
async def update_todo(todo_id: str, updated_todo: TodoModel):
    config = await config_manager.get_config()
    todos = config.get("todos", [])
    todo_id = todo_id.strip()

    index = -1
    for i, t in enumerate(todos):
        if t["id"] == todo_id:
            index = i
            break

    if index == -1:
        raise HTTPException(status_code=404, detail="未找到该待办任务。")

    todo_dict = updated_todo.dict()
    if not todo_dict.get("created_at"):
        todo_dict["created_at"] = todos[index].get("created_at", format_datetime())

    todos[index] = todo_dict
    config["todos"] = todos
    await config_manager.save_config(config)
    return {"status": "success", "message": "待办任务更新成功！", "todo": todo_dict}

@app.delete("/api/todos/{todo_id}", dependencies=[Depends(verify_token)])
async def delete_todo(todo_id: str):
    config = await config_manager.get_config()
    todo_id = todo_id.strip()

    todos = config.get("todos", [])
    new_todos = [t for t in todos if t["id"] != todo_id]
    if len(new_todos) == len(todos):
        raise HTTPException(status_code=404, detail="未找到该待办任务。")

    config["todos"] = new_todos
    await config_manager.save_config(config)
    return {"status": "success", "message": "待办任务删除成功！"}

@app.post("/api/todos/{todo_id}/toggle", dependencies=[Depends(verify_token)])
async def toggle_todo(todo_id: str):
    config = await config_manager.get_config()
    todo_id = todo_id.strip()

    todo = next((t for t in config.get("todos", []) if t["id"] == todo_id), None)
    if not todo:
        raise HTTPException(status_code=404, detail="未找到该待办任务。")

    todo["is_enabled"] = not todo.get("is_enabled", True)
    await config_manager.save_config(config)
    status_str = "启用" if todo["is_enabled"] else "暂停"
    return {"status": "success", "message": f"待办任务已{status_str}。", "is_enabled": todo["is_enabled"]}

@app.post("/api/todos/{todo_id}/complete", dependencies=[Depends(verify_token)])
async def complete_todo(todo_id: str):
    config = await config_manager.get_config()
    todo_id = todo_id.strip()

    todo = next((t for t in config.get("todos", []) if t["id"] == todo_id), None)
    if not todo:
        raise HTTPException(status_code=404, detail="未找到该待办任务。")

    now = datetime.now()
    now_str = format_datetime(now)

    if todo.get("is_recurring", False):
        target_str = todo.get("target_date", "")
        target_dt = parse_target_datetime(target_str) or now
        next_dt = advance_target_to_future(
            target_dt,
            todo.get("repeat_interval_value", 1),
            todo.get("repeat_interval_unit", "days"),
            now
        )
        todo["target_date"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
        todo["status"] = "pending"
        todo["completed_at"] = now_str
        todo["last_remind_time"] = None
        message = f"待办「{todo.get('title')}」已完成！已自动调度至下一周期：{todo['target_date']}"
    else:
        todo["status"] = "completed"
        todo["completed_at"] = now_str
        todo["last_remind_time"] = None
        message = f"待办「{todo.get('title')}」已确认完成！"

    await config_manager.save_config(config)
    return {"status": "success", "message": message, "todo": todo}

@app.post("/api/todos/{todo_id}/reset", dependencies=[Depends(verify_token)])
async def reset_todo(todo_id: str, payload: Optional[Dict[str, Any]] = None):
    config = await config_manager.get_config()
    todo_id = todo_id.strip()

    todo = next((t for t in config.get("todos", []) if t["id"] == todo_id), None)
    if not todo:
        raise HTTPException(status_code=404, detail="未找到该待办任务。")

    if payload and "target_date" in payload and payload["target_date"]:
        todo["target_date"] = payload["target_date"]
    todo["status"] = "pending"
    todo["completed_at"] = None
    todo["last_remind_time"] = None
    todo["is_enabled"] = True

    await config_manager.save_config(config)
    return {"status": "success", "message": f"待办「{todo.get('title')}」已重置并重新激活！", "todo": todo}

@app.post("/api/todos/{todo_id}/trigger-test", dependencies=[Depends(verify_token)])
async def trigger_test_todo(todo_id: str):
    config = await config_manager.get_config()
    todo_id = todo_id.strip()

    todo = next((t for t in config.get("todos", []) if t["id"] == todo_id), None)
    if not todo:
        raise HTTPException(status_code=404, detail="未找到该待办任务。")

    asyncio.create_task(todo_manager.trigger_todo_webhook(todo, is_retry=False))
    return {"status": "success", "message": f"已向待办「{todo.get('title')}」的 Webhook 发送测试提醒！"}


# ----------------- 系统版本与在线更新检测/触发 API -----------------

_raw_ver = os.getenv("APP_VERSION", "v1.2.0")
APP_VERSION = "v1.2.0" if _raw_ver in ("main", "dev", "") else _raw_ver
APP_COMMIT_SHA = os.getenv("APP_COMMIT_SHA", "")

def get_current_version_info() -> Dict[str, str]:
    """获取当前系统运行的版本号与 Commit SHA"""
    sha = APP_COMMIT_SHA
    if not sha or sha == "dev":
        try:
            import subprocess
            out = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, timeout=2).decode().strip()
            if out:
                sha = out
        except Exception:
            pass
    if not sha:
        sha = "12363f1"
    return {
        "version": APP_VERSION,
        "commit_sha": sha,
        "commit_short": sha[:7] if sha else "unknown"
    }

async def check_github_update() -> Dict[str, Any]:
    """检测 GitHub 远程仓库最新 Commit"""
    current = get_current_version_info()
    current_sha = current["commit_sha"]
    url = "https://api.github.com/repos/tinet-jutt/tgMsgMonitor/commits/main"
    headers = {
        "User-Agent": "tgMsgMonitor-AutoUpdater/1.0",
        "Accept": "application/vnd.github.v3+json"
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                latest_sha = data.get("sha", "")
                commit_info = data.get("commit", {})
                message = commit_info.get("message", "").strip()
                date_str = commit_info.get("committer", {}).get("date") or commit_info.get("author", {}).get("date", "")
                
                # 转换显示时间为标准格式
                if date_str:
                    try:
                        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
                        dt = dt + timedelta(hours=8)
                        date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pass

                has_update = (latest_sha[:7].lower() != current_sha[:7].lower()) if (latest_sha and current_sha) else False
                return {
                    "status": "success",
                    "has_update": has_update,
                    "current_version": current["version"],
                    "current_commit": current_sha,
                    "current_commit_short": current["commit_short"],
                    "latest_commit": latest_sha,
                    "latest_commit_short": latest_sha[:7] if latest_sha else "",
                    "commit_message": message,
                    "commit_date": date_str,
                    "commit_url": data.get("html_url", ""),
                    "check_time": format_datetime()
                }
            else:
                return {
                    "status": "error",
                    "has_update": False,
                    "error": f"GitHub API 返回状态码 {resp.status_code}",
                    "current_version": current["version"],
                    "current_commit_short": current["commit_short"]
                }
    except Exception as e:
        logger.error(f"检查更新异常: {e}")
        return {
            "status": "error",
            "has_update": False,
            "error": f"连接 GitHub 失败: {str(e)}",
            "current_version": current["version"],
            "current_commit_short": current["commit_short"]
        }

async def trigger_system_update() -> Dict[str, Any]:
    """主动触发系统镜像拉取更新与平滑重建"""
    # 1. 优先使用 Watchtower HTTP API 触发容器镜像更新
    watchtower_url = os.getenv("WATCHTOWER_API_URL", "http://172.17.0.1:8088/v1/update")
    watchtower_token = os.getenv("WATCHTOWER_API_TOKEN", "admin123-update-token")
    headers = {"Authorization": f"Bearer {watchtower_token}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(watchtower_url, headers=headers)
            if resp.status_code in (200, 204):
                logger.info("已成功向 Watchtower 发送更新指令！")
                return {
                    "status": "success",
                    "mode": "watchtower",
                    "message": "已向 Watchtower 触发自动更新指令，系统正在后台拉取最新镜像并平滑重建容器，请在 10~20 秒后刷新页面。"
                }
            else:
                logger.warning(f"Watchtower API 返回 HTTP {resp.status_code}")
    except Exception as e:
        logger.info(f"Watchtower API 连接失败 ({e})，尝试检测本地环境...")

    # 2. 如果在本地 Git 开发环境直接运行
    try:
        import subprocess
        is_git_repo = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0
        if is_git_repo:
            pull_res = subprocess.run(["git", "pull", "origin", "main"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if pull_res.returncode == 0:
                return {
                    "status": "success",
                    "mode": "git",
                    "message": f"Git 代码更新成功：{pull_res.stdout.strip()}。"
                }
    except Exception:
        pass

    # 3. 兜底提示手动更新指令
    return {
        "status": "manual_required",
        "mode": "manual",
        "message": "未能直接调用 Watchtower 自动更新。您可以在服务器终端执行以下命令拉取最新镜像并重启：",
        "command": "cd /root/APP/tgMsgMonitor && docker compose pull && docker compose up -d"
    }

@app.get("/api/system/version", dependencies=[Depends(verify_token)])
async def get_system_version():
    """获取当前系统运行版本信息"""
    return get_current_version_info()

@app.get("/api/system/check-update", dependencies=[Depends(verify_token)])
async def check_update():
    """在线检测 GitHub 是否有新版本发布"""
    return await check_github_update()

@app.post("/api/system/trigger-update", dependencies=[Depends(verify_token)])
async def trigger_update():
    """主动触发系统在线更新"""
    return await trigger_system_update()


