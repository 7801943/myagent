"""
OnlyOffice 文档服务。

负责把 MyAgent 工作空间内的文件转换成 OnlyOffice 可打开的 editor config，
并处理 DocumentServer 下载文件、保存回调等服务端请求。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse

from myagent.utils.logging import get_logger


logger = get_logger(__name__)


DEFAULT_SUPPORTED_EXTENSIONS = [
    ".doc", ".docx", ".odt", ".rtf", ".txt",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
    ".pdf",
]


@dataclass
class DocumentConfig:
    """OnlyOffice 集成配置。"""

    enabled: bool = False
    onlyoffice_url: str = "http://localhost:8081"
    myagent_public_url: str = "http://localhost:8000"
    myagent_internal_url: str = "http://host.docker.internal:8000"
    access_token_ttl_seconds: int = 3600
    access_token_secret: str = ""
    onlyoffice_jwt_secret: str = ""
    onlyoffice_jwt_header: str = "Authorization"
    supported_extensions: list[str] = field(default_factory=lambda: DEFAULT_SUPPORTED_EXTENSIONS.copy())


class DocumentService:
    """
    生成 OnlyOffice 配置并保护文档下载/保存。

    这里同时使用两类 token：
      - MyAgent document token：保护 /download 与 /callback。
      - OnlyOffice JWT config.token：供 DocumentServer 验证 editor config。
    """

    def __init__(self, root_dir: str, config: dict[str, Any] | None = None):
        raw = config or {}
        self.config = DocumentConfig(
            enabled=bool(raw.get("enabled", False)),
            onlyoffice_url=str(raw.get("onlyoffice_url") or "http://localhost:8081").rstrip("/"),
            myagent_public_url=str(raw.get("myagent_public_url") or "http://localhost:8000").rstrip("/"),
            myagent_internal_url=str(raw.get("myagent_internal_url") or "http://host.docker.internal:8000").rstrip("/"),
            access_token_ttl_seconds=int(raw.get("access_token_ttl_seconds") or 3600),
            access_token_secret=str(raw.get("access_token_secret") or ""),
            onlyoffice_jwt_secret=str(raw.get("onlyoffice_jwt_secret") or ""),
            onlyoffice_jwt_header=str(raw.get("onlyoffice_jwt_header") or "Authorization"),
            supported_extensions=[
                str(ext).lower() for ext in raw.get("supported_extensions", DEFAULT_SUPPORTED_EXTENSIONS)
            ],
        )
        self.root_dir = Path(root_dir or ".").expanduser().resolve()
        self._access_secret = self.config.access_token_secret or self._derive_dev_secret()
        logger.info(
            "DocumentService initialized: enabled=%s root=%s onlyoffice_url=%s internal_url=%s jwt_enabled=%s",
            self.config.enabled,
            self.root_dir,
            self.config.onlyoffice_url,
            self.config.myagent_internal_url,
            bool(self.config.onlyoffice_jwt_secret),
        )

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def build_editor_config(self, relative_path: str, username: str, mode: str = "edit") -> dict[str, Any]:
        """构造前端 `new DocsAPI.DocEditor(...)` 所需配置。"""
        if not self.enabled:
            raise HTTPException(status_code=404, detail="文档预览/编辑未启用")

        path = self.resolve_workspace_path(relative_path)
        ext = path.suffix.lower()
        doc_type = self._document_type(ext)
        if doc_type == "pdf":
            mode = "view"
        elif mode not in {"edit", "view"}:
            mode = "edit"

        token = self._create_access_token(relative_path, username, mode)
        file_url = self._internal_api_url("/api/documents/download", relative_path, token)
        callback_url = self._internal_api_url("/api/documents/callback", relative_path, token)
        document_key = self._document_key(path, relative_path)

        config = {
            "document": {
                "fileType": ext.lstrip("."),
                "key": document_key,
                "title": path.name,
                "url": file_url,
                "permissions": {
                    "edit": mode == "edit",
                    "download": True,
                    "print": True,
                    "review": mode == "edit",
                },
            },
            "documentType": doc_type,
            "editorConfig": {
                "callbackUrl": callback_url,
                "lang": "zh-CN",
                "mode": mode,
                "user": {
                    "id": username or "myagent-user",
                    "name": username or "MyAgent User",
                },
            },
            "height": "100%",
            "width": "100%",
        }

        if self.config.onlyoffice_jwt_secret:
            config["token"] = self._create_onlyoffice_jwt(config)

        logger.info(
            "OnlyOffice editor config built: path=%s mode=%s type=%s key=%s file_url=%s callback_url=%s jwt=%s",
            relative_path,
            mode,
            doc_type,
            document_key,
            _safe_url(file_url),
            _safe_url(callback_url),
            bool(config.get("token")),
        )

        return {
            "config": config,
            "document_type": doc_type,
            "file_name": path.name,
            "onlyoffice_url": self.config.onlyoffice_url,
            "onlyoffice_jwt_header": self.config.onlyoffice_jwt_header,
        }

    def download_file(self, relative_path: str, token: str) -> FileResponse:
        """供 OnlyOffice DocumentServer 通过 document.url 下载原文件。"""
        token_payload = self._verify_access_token(token, relative_path)
        path = self.resolve_workspace_path(relative_path)
        logger.info(
            "OnlyOffice download accepted: path=%s user=%s size=%s token_exp=%s",
            relative_path,
            token_payload.get("username", ""),
            path.stat().st_size,
            token_payload.get("exp"),
        )
        return FileResponse(path=str(path), filename=path.name)

    async def handle_callback(self, relative_path: str, token: str, payload: dict[str, Any]) -> dict[str, int]:
        """处理 OnlyOffice 保存回调。status=2/6 时下载新文件并原子覆盖。"""
        token_payload = self._verify_access_token(token, relative_path)
        status = int(payload.get("status") or 0)
        logger.info(
            "OnlyOffice callback received: path=%s status=%s user=%s has_url=%s payload_keys=%s",
            relative_path,
            status,
            token_payload.get("username", ""),
            bool(payload.get("url")),
            sorted(payload.keys()),
        )
        if status not in {2, 6}:
            logger.info("OnlyOffice callback ignored: path=%s status=%s", relative_path, status)
            return {"error": 0}

        download_url = payload.get("url")
        if not download_url:
            logger.warning("OnlyOffice callback missing download url: path=%s status=%s", relative_path, status)
            return {"error": 1}

        path = self.resolve_workspace_path(relative_path)
        tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")

        # OnlyOffice 回调里的 url 是一次性下载地址，需要服务端立即拉取。
        try:
            logger.info(
                "OnlyOffice callback downloading updated file: path=%s url=%s",
                relative_path,
                _safe_url(str(download_url)),
            )
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(str(download_url))
                response.raise_for_status()
            logger.info(
                "OnlyOffice callback downloaded updated file: path=%s status_code=%s bytes=%s",
                relative_path,
                response.status_code,
                len(response.content),
            )
            tmp_path.write_bytes(response.content)
            os.replace(tmp_path, path)
            logger.info(
                "OnlyOffice callback saved file: path=%s absolute_path=%s bytes=%s",
                relative_path,
                path,
                path.stat().st_size,
            )
        except Exception:
            logger.exception("OnlyOffice callback failed to save file: path=%s", relative_path)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            finally:
                return {"error": 1}

        return {"error": 0}

    def resolve_workspace_path(self, relative_path: str) -> Path:
        """解析并校验 workspace 相对路径，禁止越界和目录访问。"""
        if not relative_path or Path(relative_path).is_absolute():
            raise HTTPException(status_code=400, detail="文件路径必须是工作区相对路径")

        normalized = relative_path.replace("\\", "/").strip("/")
        path = (self.root_dir / normalized).resolve()
        if path != self.root_dir and self.root_dir not in path.parents:
            raise HTTPException(status_code=403, detail="文件不在工作区内")
        if not path.exists() or path.is_dir():
            raise HTTPException(status_code=404, detail="文件不存在")
        if path.suffix.lower() not in self.config.supported_extensions:
            raise HTTPException(status_code=415, detail="OnlyOffice 不支持该文件类型")
        return path

    def _internal_api_url(self, route: str, relative_path: str, token: str) -> str:
        from urllib.parse import quote

        return (
            f"{self.config.myagent_internal_url}{route}"
            f"?path={quote(relative_path, safe='')}&token={quote(token, safe='')}"
        )

    def _create_access_token(self, relative_path: str, username: str, mode: str) -> str:
        now = int(time.time())
        payload = {
            "path": relative_path,
            "username": username,
            "mode": mode,
            "iat": now,
            "exp": now + self.config.access_token_ttl_seconds,
            "nonce": secrets.token_urlsafe(12),
        }
        return self._sign_payload(payload, self._access_secret)

    def _verify_access_token(self, token: str, relative_path: str) -> dict[str, Any]:
        payload = self._verify_signed_payload(token, self._access_secret)
        if payload.get("path") != relative_path:
            raise HTTPException(status_code=403, detail="文档 token 与路径不匹配")
        if int(payload.get("exp") or 0) < int(time.time()):
            raise HTTPException(status_code=403, detail="文档 token 已过期")
        return payload

    def _create_onlyoffice_jwt(self, config: dict[str, Any]) -> str:
        # OnlyOffice 期望 config.token 是包含完整 editor config 的 HS256 JWT。
        return self._sign_payload(config, self.config.onlyoffice_jwt_secret)

    @staticmethod
    def _sign_payload(payload: dict[str, Any], secret: str) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        header_b64 = _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        payload_b64 = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        return f"{header_b64}.{payload_b64}.{_b64url(signature)}"

    @staticmethod
    def _verify_signed_payload(token: str, secret: str) -> dict[str, Any]:
        try:
            header_b64, payload_b64, signature_b64 = token.split(".", 2)
            signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
            expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
            actual = _b64url_decode(signature_b64)
            if not hmac.compare_digest(expected, actual):
                raise ValueError("bad signature")
            return json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=403, detail="无效的文档 token") from exc

    def _derive_dev_secret(self) -> str:
        seed = f"{self.root_dir}:{self.config.myagent_internal_url}:myagent-documents"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    @staticmethod
    def _document_key(path: Path, relative_path: str) -> str:
        stat = path.stat()
        raw = f"{relative_path}:{stat.st_mtime_ns}:{stat.st_size}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]

    @staticmethod
    def _document_type(ext: str) -> str:
        if ext in {".doc", ".docx", ".odt", ".rtf", ".txt"}:
            return "word"
        if ext in {".xls", ".xlsx", ".ods", ".csv"}:
            return "cell"
        if ext in {".ppt", ".pptx", ".odp"}:
            return "slide"
        if ext == ".pdf":
            return "pdf"
        raise HTTPException(status_code=415, detail="OnlyOffice 不支持该文件类型")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _safe_url(url: str) -> str:
    """隐藏 URL 中的 token，只保留可诊断的路由和普通查询参数。"""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in {"token", "jwt", "access_token"}:
            query.append((key, _fingerprint(value)))
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _fingerprint(value: str) -> str:
    if not value:
        return "<empty>"
    return f"<sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]}>"
