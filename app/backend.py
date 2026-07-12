import os
import json
import asyncio
import re
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Depends, Header, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from loguru import logger
import httpx
import secrets
import time

# 基础目录配置
CONFIG_PATH = "config.json"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

import urllib.parse

# ----------------- 占位符解析工具函数 -----------------

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

# ----------------- Pydantic 模型 -----------------

class GlobalWebhookConfig(BaseModel):
    url: str = ""
    timeout: int = 10
    method: str = "POST"  # GET / POST
    custom_body: str = ""

class RuleFilter(BaseModel):
    keywords: List[str] = []
    exclude_keywords: List[str] = []
    use_regex: bool = False

class RuleWebhook(BaseModel):
    url: str = ""
    method: str = ""  # 为空时继承全局
    custom_body: str = ""

class RuleModel(BaseModel):
    id: str
    name: str
    accounts: List[str] = ["all"]  # 绑定手机号列表或 ["all"]
    targets: List[str] = []         # 监听的目标，例如 ["@group", "-1001234567"]
    filters: RuleFilter
    webhook: RuleWebhook
    is_enabled: bool = True

class AccountModel(BaseModel):
    phone: str
    api_id: int
    api_hash: str
    session_name: str
    is_active: bool = False

class SystemConfig(BaseModel):
    accounts: List[AccountModel] = []
    global_webhook: GlobalWebhookConfig = GlobalWebhookConfig()
    rules: List[RuleModel] = []

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
            "rules": []
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
                    client = TelegramClient(session_path, api_id, api_hash)
                    await client.connect()
                    
                    if await client.is_user_authorized():
                        self.active_clients[phone] = client
                        self.register_handlers(phone, client)
                        logger.info(f"账号 {phone} 自动连接并监听成功。")
                    else:
                        logger.warning(f"账号 {phone} 已失效或未授权，标记为未激活。")
                        await client.disconnect()
                        # 更新配置状态
                        acc["is_active"] = False
                        await self.config_manager.save_config(config)
                except Exception as e:
                    logger.error(f"初始化账号 {phone} 失败: {e}")

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

            # 5. 触发 Webhook
            rule_webhook = rule.get("webhook", {})
            webhook_url = rule_webhook.get("url") or global_webhook.get("url")
            webhook_timeout = global_webhook.get("timeout", 10)
            webhook_method = rule_webhook.get("method") or global_webhook.get("method") or "POST"
            webhook_custom_body = rule_webhook.get("custom_body") if rule_webhook.get("url") else global_webhook.get("custom_body") or ""

            # 整理占位符所需的数据
            message_date = event.message.date.isoformat() if (event.message and event.message.date) else ""
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

            # B. Username 匹配 (带或不带 @) - 会话匹配
            username_to_check = target_str[1:] if target_str.startswith('@') else target_str
            if chat and hasattr(chat, 'username') and chat.username:
                if chat.username.lower() == username_to_check.lower():
                    return True

            # C. 发送人 (Sender) Username 匹配 (带或不带 @) - 允许直接按人/机器人监控
            if sender and hasattr(sender, 'username') and sender.username:
                if sender.username.lower() == username_to_check.lower():
                    return True

            # D. 聊天 Title 标题匹配 (模糊匹配或全匹配，这里做全匹配)
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
                        message_date = event.message.date.isoformat() if (event.message and event.message.date) else ""

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

# ----------------- FastAPI 初始化与路由 -----------------

app = FastAPI(title="Telegram Message Monitor Admin API")

config_manager = ConfigManager()
tg_manager = TelegramManager(config_manager)

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

@app.on_event("shutdown")
async def shutdown_event():
    await tg_manager.cleanup()

# --- 账号管理 API ---

@app.get("/api/accounts", dependencies=[Depends(verify_token)])
async def get_accounts():
    config = await config_manager.get_config()
    accounts = config.get("accounts", [])
    
    result = []
    for acc in accounts:
        phone = acc["phone"]
        # 动态查询连接状态
        status = "offline"
        if phone in tg_manager.active_clients:
            client = tg_manager.active_clients[phone]
            if client.is_connected() and await client.is_user_authorized():
                status = "online"
        elif phone in tg_manager.login_clients:
            status = "logging_in"
            
        result.append({
            "phone": phone,
            "api_id": acc["api_id"],
            "status": status,
            "is_active": acc.get("is_active", False)
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

# --- Webhook 测试联调 API ---

class TestWebhookReq(BaseModel):
    url: str
    method: str = "POST"
    custom_body: str = ""

@app.post("/api/webhook/test", dependencies=[Depends(verify_token)])
async def test_webhook(req: TestWebhookReq):
    url = req.url.strip()
    method = req.method.strip().upper()
    custom_body = req.custom_body

    if not url:
        raise HTTPException(status_code=400, detail="Webhook URL 不能为空。")

    # Mock 占位符数据
    mock_placeholders = {
        "text": "这是一条来自测试按钮的测试消息内容。",
        "msg_id": 99999,
        "date": "2026-07-11T21:48:47+08:00",
        "sender_id": 12345678,
        "sender_username": "test_sender",
        "sender_name": "测试发送人",
        "chat_id": -100123456,
        "chat_title": "测试监控群组",
        "chat_username": "test_group",
        "receiver_account": "+8613800000000",
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
