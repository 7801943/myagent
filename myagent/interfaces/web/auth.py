"""
认证引擎：用户加载、密码校验、Token 管理、IP 限制。

功能：
  - PBKDF2-SHA256 密码哈希与校验
  - Token 生成、验证、过期管理（TTL）
  - 用户 IP 绑定与数量限制
  - IPv4 / IPv6 兼容的 IP 标准化
  - 反向代理场景下的真实 IP 获取
  - 用户数据 JSON 文件持久化
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from myagent.utils.logging import get_logger

logger = get_logger(__name__)

# ── 默认配置 ──

DEFAULT_TOKEN_TTL = 86400       # 24 小时
DEFAULT_MAX_IPS = 2
DEFAULT_ITERATIONS = 100000
DEFAULT_USERS_FILE = "data/users.json"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"


# ── 密码哈希 ──

def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    """使用 PBKDF2-SHA256 生成密码哈希。格式：pbkdf2_sha256$iterations$salt$hash"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    hash_hex = dk.hex()
    return f"pbkdf2_sha256${iterations}${salt}${hash_hex}"


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码是否匹配哈希值。"""
    try:
        parts = password_hash.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        _, iterations_str, salt, stored_hash = parts
        iterations = int(iterations_str)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
        return secrets.compare_digest(dk.hex(), stored_hash)
    except (ValueError, IndexError):
        return False


# ── IP 标准化 ──

def normalize_ip(raw_ip: str) -> str:
    """
    标准化 IP 地址。

    - IPv4-mapped IPv6（如 ::ffff:192.168.1.1）→ IPv4 格式（192.168.1.1）
    - IPv6 地址统一为完整小写形式
    - IPv4 地址保持原样
    """
    if not raw_ip:
        return raw_ip

    try:
        addr = ipaddress.ip_address(raw_ip)
        # IPv4-mapped IPv6 地址转换为纯 IPv4
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(addr)
    except ValueError:
        # 无法解析时返回原始值（兼容性处理）
        logger.warning(f"Cannot parse IP address: {raw_ip}")
        return raw_ip


def get_client_ip(request: Any, trusted_proxies: list[str] | None = None) -> str:
    """
    从请求中获取客户端真实 IP。

    优先级：
      1. X-Forwarded-For（第一个地址，最原始的客户端）
      2. X-Real-IP
      3. request.client.host（直连 IP）

    Args:
        request: FastAPI Request 对象
        trusted_proxies: 信任的代理地址列表，为 None 时不信任任何代理头
    """
    direct_ip = request.client.host if request.client else "127.0.0.1"
    direct_ip = normalize_ip(direct_ip)

    # 如果没有配置信任代理，直接使用直连 IP
    if not trusted_proxies:
        return direct_ip

    # 检查直连 IP 是否在信任列表中
    is_trusted = False
    for proxy in trusted_proxies:
        try:
            if normalize_ip(direct_ip) == normalize_ip(proxy):
                is_trusted = True
                break
        except ValueError:
            continue

    if not is_trusted:
        return direct_ip

    # 信任代理 → 尝试从头部获取真实 IP
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        # X-Forwarded-For: client, proxy1, proxy2 → 取第一个
        first_ip = xff.split(",")[0].strip()
        if first_ip:
            return normalize_ip(first_ip)

    x_real_ip = request.headers.get("x-real-ip", "")
    if x_real_ip:
        return normalize_ip(x_real_ip)

    return direct_ip


def get_ws_client_ip(websocket: Any, trusted_proxies: list[str] | None = None) -> str:
    """
    从 WebSocket 连接中获取客户端真实 IP（逻辑与 HTTP 相同）。
    """
    direct_ip = websocket.client.host if websocket.client else "127.0.0.1"
    direct_ip = normalize_ip(direct_ip)

    if not trusted_proxies:
        return direct_ip

    is_trusted = False
    for proxy in trusted_proxies:
        try:
            if normalize_ip(direct_ip) == normalize_ip(proxy):
                is_trusted = True
                break
        except ValueError:
            continue

    if not is_trusted:
        return direct_ip

    xff = websocket.headers.get("x-forwarded-for", "")
    if xff:
        first_ip = xff.split(",")[0].strip()
        if first_ip:
            return normalize_ip(first_ip)

    x_real_ip = websocket.headers.get("x-real-ip", "")
    if x_real_ip:
        return normalize_ip(x_real_ip)

    return direct_ip


# ── 数据模型 ──

@dataclass
class TokenInfo:
    """Token 信息。"""
    token: str
    username: str
    ip: str
    created_at: float  # time.time()


@dataclass
class UserData:
    """用户数据。"""
    username: str
    password_hash: str
    active_ips: list[str] = field(default_factory=list)


# ── AuthService ──

class AuthService:
    """
    认证服务：管理用户、Token、IP 限制。

    用法：
        service = AuthService(users_file="data/users.json")
        service.load_users()

        # 登录
        token_info = service.login(username, password, client_ip)

        # 验证 token
        info = service.validate_token(token_string)

        # 登出
        service.logout(token_string)
    """

    def __init__(
        self,
        users_file: str = DEFAULT_USERS_FILE,
        token_ttl: int = DEFAULT_TOKEN_TTL,
        max_ips: int = DEFAULT_MAX_IPS,
        trusted_proxies: list[str] | None = None,
    ):
        self._users_file = Path(users_file)
        self._token_ttl = token_ttl
        self._max_ips = max_ips
        self._trusted_proxies = trusted_proxies

        # 用户数据 {username: UserData}
        self._users: dict[str, UserData] = {}
        # Token 存储 {token_string: TokenInfo}
        self._tokens: dict[str, TokenInfo] = {}
        # 文件读写锁
        self._file_lock = asyncio.Lock()

    @property
    def token_ttl(self) -> int:
        return self._token_ttl

    @property
    def max_ips(self) -> int:
        return self._max_ips

    @property
    def trusted_proxies(self) -> list[str] | None:
        return self._trusted_proxies

    # ── 用户数据管理 ──

    def load_users(self) -> None:
        """从 JSON 文件加载用户数据。如果文件不存在则创建默认用户。"""
        if not self._users_file.exists():
            logger.info(f"Users file not found: {self._users_file}, creating default user")
            self._create_default_users_file()
            return

        try:
            with open(self._users_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load users file: {e}, creating default")
            self._create_default_users_file()
            return

        self._users.clear()
        for user_data in data.get("users", []):
            username = user_data.get("username", "")
            if not username:
                continue
            self._users[username] = UserData(
                username=username,
                password_hash=user_data.get("password_hash", ""),
                active_ips=user_data.get("active_ips", []),
            )

        # 从 active_ips 恢复 token 映射（启动时不恢复，需要重新登录）
        logger.info(f"Loaded {len(self._users)} users from {self._users_file}")

    async def _save_users(self) -> None:
        """将用户数据持久化到 JSON 文件。"""
        async with self._file_lock:
            data = {
                "users": [
                    {
                        "username": u.username,
                        "password_hash": u.password_hash,
                        "active_ips": list(u.active_ips),
                    }
                    for u in self._users.values()
                ]
            }

            # 确保目录存在
            self._users_file.parent.mkdir(parents=True, exist_ok=True)

            # 原子写入：先写临时文件，再重命名
            tmp_file = self._users_file.with_suffix(".tmp")
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                # 在同一文件系统上重命名是原子的
                os.replace(tmp_file, self._users_file)
            except OSError as e:
                logger.error(f"Failed to save users file: {e}")
                if tmp_file.exists():
                    tmp_file.unlink()

    def _create_default_users_file(self) -> None:
        """创建默认用户数据文件。"""
        default_hash = hash_password(DEFAULT_PASSWORD)
        self._users = {
            DEFAULT_USERNAME: UserData(
                username=DEFAULT_USERNAME,
                password_hash=default_hash,
                active_ips=[],
            )
        }

        # 同步写入文件
        self._users_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "users": [
                {
                    "username": DEFAULT_USERNAME,
                    "password_hash": default_hash,
                    "active_ips": [],
                }
            ]
        }
        try:
            with open(self._users_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"Created default users file: {self._users_file}")
            logger.warning(
                f"⚠️  Default user created: {DEFAULT_USERNAME}/{DEFAULT_PASSWORD} — "
                "PLEASE CHANGE THE PASSWORD IMMEDIATELY!"
            )
        except OSError as e:
            logger.error(f"Failed to create default users file: {e}")

    # ── Token 管理 ──

    def _cleanup_expired_tokens(self) -> None:
        """清理过期的 Token（懒清理）。"""
        now = time.time()
        expired = [
            t for t, info in self._tokens.items()
            if now - info.created_at > self._token_ttl
        ]
        for t in expired:
            info = self._tokens.pop(t)
            # 从用户的 active_ips 中移除（如果没有其他 token 使用同一 IP）
            self._remove_ip_if_unused(info.username, info.ip)

    def _remove_ip_if_unused(self, username: str, ip: str) -> None:
        """如果该 IP 没有其他 token 在使用，则从 active_ips 中移除。"""
        user = self._users.get(username)
        if not user:
            return

        # 检查是否还有其他 token 使用这个 IP
        still_active = any(
            info.username == username and info.ip == ip
            for info in self._tokens.values()
        )
        if not still_active and ip in user.active_ips:
            user.active_ips.remove(ip)

    # ── 核心认证方法 ──

    async def login(self, username: str, password: str, client_ip: str) -> tuple[TokenInfo | None, str]:
        """
        用户登录。

        Returns:
            (TokenInfo | None, error_message)
            成功时 TokenInfo 不为 None，error_message 为空
            失败时 TokenInfo 为 None，error_message 为错误原因
        """
        client_ip = normalize_ip(client_ip)

        # 1. 检查用户是否存在
        user = self._users.get(username)
        if not user:
            return None, "用户名或密码错误"

        # 2. 校验密码
        if not verify_password(password, user.password_hash):
            return None, "用户名或密码错误"

        # 3. 清理过期 Token
        self._cleanup_expired_tokens()

        # 4. 检查 IP 限制
        if client_ip not in user.active_ips:
            if len(user.active_ips) >= self._max_ips:
                return None, f"已达到最大IP连接数({self._max_ips})，当前已绑定IP: {', '.join(user.active_ips)}"

        # 5. 生成 Token
        token_str = secrets.token_urlsafe(32)
        token_info = TokenInfo(
            token=token_str,
            username=username,
            ip=client_ip,
            created_at=time.time(),
        )
        self._tokens[token_str] = token_info

        # 6. 绑定 IP
        if client_ip not in user.active_ips:
            user.active_ips.append(client_ip)

        # 7. 持久化
        await self._save_users()

        logger.info(f"User '{username}' logged in from IP: {client_ip}")
        return token_info, ""

    async def logout(self, token_str: str) -> bool:
        """
        用户登出。

        Returns:
            True 表示成功登出，False 表示 token 无效
        """
        info = self._tokens.pop(token_str, None)
        if not info:
            return False

        # 移除 IP 绑定（如果没有其他 token 使用同一 IP）
        self._remove_ip_if_unused(info.username, info.ip)

        # 持久化
        await self._save_users()

        logger.info(f"User '{info.username}' logged out, IP: {info.ip}")
        return True

    def validate_token(self, token_str: str) -> TokenInfo | None:
        """
        验证 Token 是否有效。

        Returns:
            有效时返回 TokenInfo，无效或过期返回 None
        """
        info = self._tokens.get(token_str)
        if not info:
            return None

        # 检查过期
        if time.time() - info.created_at > self._token_ttl:
            # 清理过期 token
            self._tokens.pop(token_str, None)
            return None

        return info

    def validate_ws_token(self, token_str: str) -> TokenInfo | None:
        """验证 WebSocket 连接的 token（逻辑与 HTTP 相同）。"""
        return self.validate_token(token_str)

    def get_user_info(self, username: str) -> dict | None:
        """获取用户信息。"""
        user = self._users.get(username)
        if not user:
            return None
        return {
            "username": user.username,
            "active_ips": list(user.active_ips),
        }

    def get_active_tokens_count(self, username: str) -> int:
        """获取某用户当前的活跃 token 数量。"""
        return sum(1 for info in self._tokens.values() if info.username == username)