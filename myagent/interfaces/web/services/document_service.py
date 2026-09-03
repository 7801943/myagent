"""
OnlyOffice 文档服务。

负责把 z工作台工作空间内的文件转换成 OnlyOffice 可打开的 editor config，
并处理 DocumentServer 下载文件、保存回调等服务端请求。
"""
from __future__ import annotations

import asyncio
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
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse

from myagent.interfaces.web.private_proxy_context import normalize_loopback_http_origin
from myagent.integrations.onlyoffice.protocol import PLUGIN_GUID, PROTOCOL_VERSION
from myagent.utils.logging import get_logger


logger = get_logger(__name__)


DEFAULT_SUPPORTED_EXTENSIONS = [
    ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".markdown",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
    ".pdf",
]


@dataclass
class DocumentConfig:
    """OnlyOffice 集成配置。"""

    enabled: bool = False
    onlyoffice_url: str = "/onlyoffice"
    onlyoffice_internal_url: str = "http://localhost:8081"
    myagent_public_url: str = "http://localhost:8000"
    myagent_internal_url: str = "http://host.docker.internal:8000"
    access_token_ttl_seconds: int = 3600
    access_token_secret: str = ""
    onlyoffice_jwt_secret: str = ""
    onlyoffice_jwt_header: str = "Authorization"
    supported_extensions: list[str] = field(default_factory=lambda: DEFAULT_SUPPORTED_EXTENSIONS.copy())
    automation_enabled: bool = False
    automation_save_timeout_seconds: float = 30.0


class DocumentService:
    """
    生成 OnlyOffice 配置并保护文档下载/保存。

    这里同时使用两类 token：
      - z-workbench document token：保护 /download 与 /callback。
      - OnlyOffice JWT config.token：供 DocumentServer 验证 editor config。
    """

    def __init__(self, root_dir: str, config: dict[str, Any] | None = None):
        raw = config or {}
        onlyoffice_url = _normalize_url_base(str(raw.get("onlyoffice_url") or "/onlyoffice"))
        self.config = DocumentConfig(
            enabled=bool(raw.get("enabled", False)),
            onlyoffice_url=onlyoffice_url,
            onlyoffice_internal_url=_normalize_url_base(
                str(
                    raw.get("onlyoffice_internal_url")
                    or raw.get("onlyoffice_upstream_url")
                    or _default_onlyoffice_internal_url(onlyoffice_url)
                )
            ),
            myagent_public_url=str(raw.get("myagent_public_url") or "http://localhost:8000").rstrip("/"),
            myagent_internal_url=str(raw.get("myagent_internal_url") or "http://host.docker.internal:8000").rstrip("/"),
            access_token_ttl_seconds=int(raw.get("access_token_ttl_seconds") or 3600),
            access_token_secret=str(raw.get("access_token_secret") or ""),
            onlyoffice_jwt_secret=str(raw.get("onlyoffice_jwt_secret") or ""),
            onlyoffice_jwt_header=str(raw.get("onlyoffice_jwt_header") or "Authorization"),
            supported_extensions=[
                str(ext).lower() for ext in raw.get("supported_extensions", DEFAULT_SUPPORTED_EXTENSIONS)
            ],
            automation_enabled=bool((raw.get("automation") or {}).get("enabled", False)),
            automation_save_timeout_seconds=float(
                (raw.get("automation") or {}).get("save_timeout_seconds", 30)
            ),
        )
        self.root_dir = Path(root_dir or ".").expanduser().resolve()
        self._access_secret = self.config.access_token_secret or self._derive_dev_secret()
        self._document_leases: dict[str, tuple[str, float]] = {}
        self._save_waiters: dict[str, asyncio.Future] = {}
        logger.info(
            "DocumentService initialized: enabled=%s root=%s onlyoffice_url=%s onlyoffice_internal_url=%s "
            "myagent_internal_url=%s jwt_enabled=%s",
            self.config.enabled,
            self.root_dir,
            self.config.onlyoffice_url,
            self.config.onlyoffice_internal_url,
            self.config.myagent_internal_url,
            bool(self.config.onlyoffice_jwt_secret),
        )

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def build_editor_config(
        self,
        relative_path: str,
        username: str,
        mode: str = "edit",
        workspace_root: str | None = None,
        session_id: str = "",
        group: str = "user",
        resolver=None,
        onlyoffice_proxy_origin: str | None = None,
        browser_origin: str | None = None,
    ) -> dict[str, Any]:
        """构造前端 `new DocsAPI.DocEditor(...)` 所需配置。"""
        if not self.enabled:
            raise HTTPException(status_code=404, detail="文档预览/编辑未启用")
        onlyoffice_proxy_origin = normalize_loopback_http_origin(onlyoffice_proxy_origin)
        browser_origin = _normalize_http_origin(browser_origin)
        if _is_absolute_http_url(self.config.onlyoffice_url):
            onlyoffice_proxy_origin = None
        path, scope = self.resolve_document_path(relative_path, workspace_root, resolver, operation="read", actor="user")
        ext = path.suffix.lower()
        doc_type = self._document_type(ext)
        if doc_type == "pdf":
            mode = "view"
        elif mode not in {"edit", "view"}:
            mode = "edit"
        if mode == "edit" and resolver is not None:
            try:
                resolved_for_write = resolver.resolve(relative_path, operation="write", actor="onlyoffice", must_exist=True)
                scope = resolved_for_write.area
            except Exception:
                mode = "view"

        document_key = self._leased_document_key(path, relative_path)
        token = self._create_access_token(
            relative_path,
            username,
            mode,
            session_id=session_id,
            group=group,
            scope=scope,
            onlyoffice_proxy_origin=onlyoffice_proxy_origin,
            document_key=document_key,
        )
        file_url = self._internal_api_url("/api/documents/download", relative_path, token)
        callback_url = self._internal_api_url("/api/documents/callback", relative_path, token)
        editor_session_id = secrets.token_hex(16)
        bridge_nonce = secrets.token_urlsafe(32)

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
                "customization": {
                    "forcesave": mode == "edit",
                },
                "user": {
                    "id": username or "myagent-user",
                    "name": username or "z-workbench User",
                },
            },
            "height": "100%",
            "width": "100%",
        }

        automation_meta = None
        if self.config.automation_enabled and ext in {".docx", ".pdf", ".xlsx"}:
            plugin_host_origin = browser_origin or onlyoffice_proxy_origin or self.config.myagent_public_url
            plugin_payload = {
                "kind": "onlyoffice_plugin",
                "path": relative_path,
                "username": username,
                "group": group,
                "session_id": session_id,
                "editor_session_id": editor_session_id,
                "document_key": document_key,
                "nonce": bridge_nonce,
                "host_origin": plugin_host_origin,
                "writable": mode == "edit",
                "iat": int(time.time()),
                "exp": int(time.time()) + self.config.access_token_ttl_seconds,
            }
            plugin_token = self._sign_payload(plugin_payload, self._access_secret)
            # Keep the plugin manifest and entry point stable. Per-editor secrets belong
            # in ONLYOFFICE's plugins.options channel; putting them in variations[].url
            # makes the iframe URL cacheable/reusable across editor instances.
            plugin_config_url = f"{plugin_host_origin}/onlyoffice-agent-plugin/config.json"
            config["editorConfig"]["plugins"] = {
                "pluginsData": [plugin_config_url],
                "autostart": [PLUGIN_GUID],
                "options": {
                    PLUGIN_GUID: {
                        "token": plugin_token,
                    },
                },
            }
            automation_meta = {
                "protocol_version": PROTOCOL_VERSION,
                "plugin_guid": PLUGIN_GUID,
                "editor_session_id": editor_session_id,
                "document_key": document_key,
                "nonce": bridge_nonce,
                "writable": mode == "edit",
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

        response = {
            "config": config,
            "document_type": doc_type,
            "file_name": path.name,
            "onlyoffice_url": self.config.onlyoffice_url,
            "onlyoffice_jwt_header": self.config.onlyoffice_jwt_header,
        }
        if automation_meta:
            response["automation"] = automation_meta
        return response

    def download_file(
        self,
        relative_path: str,
        token: str,
        workspace_root: str | None = None,
        resolver=None,
    ) -> FileResponse:
        """供 OnlyOffice DocumentServer 通过 document.url 下载原文件。"""
        token_payload = self.verify_access_token(token, relative_path)
        path, _scope = self.resolve_document_path(relative_path, workspace_root, resolver, operation="read", actor="user")
        logger.info(
            "OnlyOffice download accepted: path=%s user=%s size=%s token_exp=%s",
            relative_path,
            token_payload.get("username", ""),
            path.stat().st_size,
            token_payload.get("exp"),
        )
        return FileResponse(path=str(path), filename=path.name)

    async def handle_callback(
        self,
        relative_path: str,
        token: str,
        payload: dict[str, Any],
        workspace_root: str | None = None,
        resolver=None,
    ) -> dict[str, int]:
        """处理 OnlyOffice 保存回调。status=2/6 时下载新文件并原子覆盖。"""
        token_payload = self.verify_access_token(token, relative_path)
        status = int(payload.get("status") or 0)
        expected_key = str(token_payload.get("document_key") or "")
        callback_key = str(payload.get("key") or "")
        if expected_key and callback_key and expected_key != callback_key:
            logger.warning("OnlyOffice callback rejected for key mismatch: path=%s", relative_path)
            return {"error": 1}
        logger.info(
            "OnlyOffice callback received: path=%s status=%s user=%s has_url=%s payload_keys=%s",
            relative_path,
            status,
            token_payload.get("username", ""),
            bool(payload.get("url")),
            sorted(payload.keys()),
        )
        if status not in {2, 6}:
            if status == 4 and callback_key:
                self.release_document_lease(relative_path, callback_key)
            logger.info("OnlyOffice callback ignored: path=%s status=%s", relative_path, status)
            return {"error": 0}
        if token_payload.get("mode") != "edit":
            logger.warning("OnlyOffice callback rejected for non-edit token: path=%s", relative_path)
            return {"error": 1}

        download_url = payload.get("url")
        if not download_url:
            logger.warning("OnlyOffice callback missing download url: path=%s status=%s", relative_path, status)
            return {"error": 1}

        path, _scope = self.resolve_document_path(
            relative_path,
            workspace_root,
            resolver,
            operation="write",
            actor="onlyoffice",
        )
        tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")

        # OnlyOffice 回调里的 url 是一次性下载地址，需要服务端立即拉取。
        try:
            resolved_download_url = self.rewrite_onlyoffice_download_url(
                str(download_url),
                trusted_proxy_origin=str(token_payload.get("onlyoffice_proxy_origin") or ""),
            )
            logger.info(
                "OnlyOffice callback downloading updated file: path=%s url=%s",
                relative_path,
                _safe_url(resolved_download_url),
            )
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
                response = await client.get(resolved_download_url)
                if response.is_redirect:
                    raise ValueError("OnlyOffice callback download redirect is not allowed")
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

    def plugin_config(self, token: str) -> dict[str, Any]:
        """Legacy signed manifest endpoint kept for already-open editor configs."""
        payload = self.verify_plugin_token(token)
        host_origin = str(payload["host_origin"]).rstrip("/")
        return {
            "name": "MyAgent ONLYOFFICE Automation Bridge",
            "guid": PLUGIN_GUID,
            "version": "1.1.0",
            "minVersion": "9.4.0",
            "baseUrl": f"{host_origin}/onlyoffice-agent-plugin/",
            "variations": [{
                "description": "Secure structured command bridge for MyAgent",
                "url": "index.html",
                "type": "unvisible",
                "isViewer": True,
                "EditorsSupport": ["word", "cell", "pdf"],
                "initDataType": "none",
                "initData": "",
                "buttons": [],
            }],
        }

    def plugin_runtime(self, token: str) -> dict[str, Any]:
        payload = self.verify_plugin_token(token)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "plugin_guid": PLUGIN_GUID,
            "path": payload["path"],
            "editor_session_id": payload["editor_session_id"],
            "document_key": payload["document_key"],
            "nonce": payload["nonce"],
            "host_origin": payload["host_origin"],
            "writable": bool(payload.get("writable")),
        }

    def verify_plugin_token(self, token: str) -> dict[str, Any]:
        payload = self._verify_signed_payload(token, self._access_secret)
        if payload.get("kind") != "onlyoffice_plugin" or int(payload.get("exp") or 0) < int(time.time()):
            raise HTTPException(status_code=403, detail="OnlyOffice 插件 token 无效或已过期")
        return payload

    async def force_save_and_wait(self, document_key: str, request_id: str, timeout: float | None = None) -> None:
        if not document_key:
            raise ValueError("document key is required")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._save_waiters[request_id] = future
        command = {"c": "forcesave", "key": document_key, "userdata": request_id}
        if self.config.onlyoffice_jwt_secret:
            command["token"] = self._create_onlyoffice_jwt(command)
        command_url = f"{self.config.onlyoffice_internal_url.rstrip('/')}/command?shardkey={document_key}"
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                response = await client.post(command_url, json=command)
                response.raise_for_status()
                data = response.json()
            error = int(data.get("error") or 0)
            if error != 0:
                raise RuntimeError(f"OnlyOffice forcesave failed with error={error}")
            await asyncio.wait_for(
                future,
                timeout=float(timeout or self.config.automation_save_timeout_seconds),
            )
        finally:
            self._save_waiters.pop(request_id, None)

    def notify_save_completed(self, payload: dict[str, Any], *, success: bool) -> None:
        request_id = str(payload.get("userdata") or "")
        future = self._save_waiters.get(request_id)
        if not future or future.done():
            return
        if success:
            future.set_result(True)
        else:
            future.set_exception(RuntimeError("OnlyOffice callback save failed"))

    def release_document_lease(self, relative_path: str, document_key: str) -> None:
        lease = self._document_leases.get(relative_path)
        if lease and lease[0] == document_key:
            self._document_leases.pop(relative_path, None)

    def _leased_document_key(self, path: Path, relative_path: str) -> str:
        lease = self._document_leases.get(relative_path)
        if lease and time.monotonic() - lease[1] < 7200:
            self._document_leases[relative_path] = (lease[0], time.monotonic())
            return lease[0]
        key = self._document_key(path, relative_path)
        self._document_leases[relative_path] = (key, time.monotonic())
        return key

    def rewrite_onlyoffice_download_url(self, download_url: str, trusted_proxy_origin: str = "") -> str:
        """
        Resolve the one-time ONLYOFFICE callback download URL to an internal DocumentServer URL.

        When the editor is loaded through the same-origin /onlyoffice proxy, DocumentServer may
        emit callback payload URLs under the public proxy base. The server should download those
        directly from the internal DocumentServer upstream and reject unrelated hosts.
        """
        raw_url = str(download_url or "").strip()
        if not raw_url:
            raise ValueError("empty OnlyOffice download URL")

        internal_base = self.config.onlyoffice_internal_url
        if _url_is_under_base(raw_url, internal_base):
            return raw_url

        normalized_private_origin = normalize_loopback_http_origin(trusted_proxy_origin)
        if normalized_private_origin and not _is_absolute_http_url(self.config.onlyoffice_url):
            private_proxy_base = _join_origin_and_path(normalized_private_origin, self.config.onlyoffice_url)
            private_cache_base = f"{private_proxy_base.rstrip('/')}/cache/files"
            if not _url_is_under_base(raw_url, private_cache_base):
                logger.warning(
                    "OnlyOffice private callback download URL rejected outside cache path: url=%s",
                    _safe_url(raw_url),
                )
                raise ValueError("OnlyOffice private download URL is not a cache file")
            suffix_path, query = _url_suffix_after_base(raw_url, private_proxy_base)
            rewritten = _join_base_and_suffix(internal_base, suffix_path, query)
            logger.info(
                "OnlyOffice callback download URL rewritten: from=%s to=%s",
                _safe_url(raw_url),
                _safe_url(rewritten),
            )
            return rewritten

        for proxy_base in _dedupe_strings(self._onlyoffice_proxy_bases()):
            if _url_is_under_base(raw_url, proxy_base):
                suffix_path, query = _url_suffix_after_base(raw_url, proxy_base)
                rewritten = _join_base_and_suffix(internal_base, suffix_path, query)
                logger.info(
                    "OnlyOffice callback download URL rewritten: from=%s to=%s",
                    _safe_url(raw_url),
                    _safe_url(rewritten),
                )
                return rewritten

        logger.warning("OnlyOffice callback download URL rejected: url=%s", _safe_url(raw_url))
        raise ValueError("OnlyOffice download URL is not trusted")

    def _onlyoffice_proxy_bases(self) -> list[str]:
        bases: list[str] = []
        browser_url = self.config.onlyoffice_url
        if _is_absolute_http_url(browser_url):
            bases.append(browser_url)
        else:
            bases.append(_join_origin_and_path(self.config.myagent_public_url, browser_url))
            bases.append(_join_origin_and_path(self.config.myagent_internal_url, browser_url))
        # In reverse-proxy deployments myagent_public_url/internal_url may already include path
        # information; keep the explicitly configured browser URL as the source of truth.
        return _dedupe_strings(bases)

    def resolve_workspace_path(self, relative_path: str, workspace_root: str | None = None) -> Path:
        """解析并校验 workspace 相对路径，禁止越界和目录访问。"""
        if not relative_path or Path(relative_path).is_absolute():
            raise HTTPException(status_code=400, detail="文件路径必须是工作区相对路径")

        normalized = relative_path.replace("\\", "/").strip("/")
        root_dir = Path(workspace_root).expanduser().resolve() if workspace_root else self.root_dir
        path = (root_dir / normalized).resolve()
        if path != root_dir and root_dir not in path.parents:
            raise HTTPException(status_code=403, detail="文件不在工作区内")
        if not path.exists() or path.is_dir():
            raise HTTPException(status_code=404, detail="文件不存在")
        if path.suffix.lower() not in self.config.supported_extensions:
            raise HTTPException(status_code=415, detail="OnlyOffice 不支持该文件类型")
        return path

    def resolve_document_path(
        self,
        relative_path: str,
        workspace_root: str | None = None,
        resolver=None,
        operation: str = "read",
        actor: str = "user",
    ) -> tuple[Path, str]:
        if resolver is not None:
            try:
                resolved = resolver.resolve(
                    relative_path,
                    operation=operation,
                    actor=actor,
                    must_exist=True,
                )
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail="文件不存在") from exc
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            path = resolved.real_path
            if path.suffix.lower() not in self.config.supported_extensions:
                raise HTTPException(status_code=415, detail="OnlyOffice 不支持该文件类型")
            return path, resolved.area
        return self.resolve_workspace_path(relative_path, workspace_root), "workspace"

    def _internal_api_url(self, route: str, relative_path: str, token: str) -> str:
        from urllib.parse import quote

        return (
            f"{self.config.myagent_internal_url}{route}"
            f"?path={quote(relative_path, safe='')}&token={quote(token, safe='')}"
        )

    def _create_access_token(
        self,
        relative_path: str,
        username: str,
        mode: str,
        session_id: str = "",
        group: str = "user",
        scope: str = "workspace",
        onlyoffice_proxy_origin: str | None = None,
        document_key: str = "",
    ) -> str:
        now = int(time.time())
        payload = {
            "path": relative_path,
            "username": username,
            "group": group,
            "scope": scope,
            "mode": mode,
            "session_id": session_id,
            "iat": now,
            "exp": now + self.config.access_token_ttl_seconds,
            "nonce": secrets.token_urlsafe(12),
        }
        if onlyoffice_proxy_origin:
            payload["onlyoffice_proxy_origin"] = onlyoffice_proxy_origin
        if document_key:
            payload["document_key"] = document_key
        return self._sign_payload(payload, self._access_secret)

    def verify_access_token(self, token: str, relative_path: str) -> dict[str, Any]:
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
        if ext in {".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".markdown"}:
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


def _normalize_url_base(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme and not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


def _default_onlyoffice_internal_url(onlyoffice_url: str) -> str:
    parsed = urlsplit(onlyoffice_url)
    if not parsed.scheme or not parsed.netloc:
        return "http://localhost:8081"
    if parsed.path.rstrip("/") == "/onlyoffice":
        return "http://localhost:8081"
    return onlyoffice_url


def _is_absolute_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_http_origin(value: str | None) -> str | None:
    """Accept an exact HTTP(S) origin and discard paths, credentials and malformed ports."""
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in str(value)):
        return None
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return None
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _join_origin_and_path(origin: str, path: str) -> str:
    origin_parts = urlsplit(_normalize_url_base(origin))
    path_parts = urlsplit(_normalize_url_base(path))
    combined_path = ""
    if origin_parts.path and origin_parts.path != "/":
        combined_path += origin_parts.path.rstrip("/")
    if path_parts.path and path_parts.path != "/":
        combined_path += "/" + path_parts.path.strip("/")
    if not combined_path:
        combined_path = "/"
    return urlunsplit((origin_parts.scheme, origin_parts.netloc, combined_path, "", ""))


def _url_is_under_base(url: str, base: str) -> bool:
    url_parts = urlsplit(url)
    base_parts = urlsplit(base)
    if url_parts.scheme.lower() != base_parts.scheme.lower() or url_parts.netloc.lower() != base_parts.netloc.lower():
        return False
    base_path = base_parts.path.rstrip("/")
    if not base_path:
        return True
    url_path = url_parts.path.rstrip("/")
    return url_path == base_path or url_parts.path.startswith(base_path + "/")


def _url_suffix_after_base(url: str, base: str) -> tuple[str, str]:
    url_parts = urlsplit(url)
    base_parts = urlsplit(base)
    base_path = base_parts.path.rstrip("/")
    suffix = url_parts.path[len(base_path):] if base_path else url_parts.path
    if not suffix:
        suffix = "/"
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    return suffix, url_parts.query


def _join_base_and_suffix(base: str, suffix_path: str, query: str = "") -> str:
    base_parts = urlsplit(base.rstrip("/"))
    base_path = base_parts.path.rstrip("/")
    suffix = "/" + suffix_path.lstrip("/")
    path = f"{base_path}{suffix}" if base_path else suffix
    return urlunsplit((base_parts.scheme, base_parts.netloc, path, query, ""))


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
