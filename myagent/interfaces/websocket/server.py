"""
WebSocket Server：集成 AgentCore，承接流式会话输出并下发给客户端。
支持全双工流式交互、工具调用展示、HITL 审批、Markdown 渲染。

启动方式：
    python -m myagent.interfaces.websocket.server
    或
    python -m myagent.interfaces.websocket.server --port 8765
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import websockets
from websockets.exceptions import ConnectionClosed

from myagent.core.agent import Agent
from myagent.core.hook import HookContext, HookManager
from myagent.core.cancellation import AgentCancelledError, CancelReason
from myagent.core.stream import StreamProcessor, StreamResult
from myagent.core.parser import StreamParser
from myagent.core.hitl import HITLController
from myagent.context.message import ToolResult as MsgToolResult
from myagent.interfaces.websocket.lock import WebSocketLock
from myagent.utils.logging import get_logger
from myagent.utils.config import load_yaml_config, AgentConfig
from myagent.providers.openai_provider import OpenAIProvider
from myagent.providers.anthropic_provider import AnthropicProvider
from myagent.providers.router import ProviderRouter
from myagent.observability.audit_logger import AuditLogger
from myagent.observability.backends.jsonl_backend import JsonlAuditBackend
from myagent.safety.guard import SafetyGuard
from myagent.safety.policy import PolicyEngine
from myagent.safety.cli_fence import CLIFence
from myagent.safety.content_rules import InputContentFilter, OutputContentFilter
from myagent.tools.registry import ToolRegistry
from myagent.tools.sandbox import SubprocessSandbox
from myagent.tools.sandbox.subprocess_sandbox import ResourceLimits
from myagent.tools.cli_tool import CLITool
from myagent.tools.file_tools import FileReadTool, FileWriteTool
from myagent.tools.secrets import SecretManager
from myagent.providers.base import StreamEvent

logger = get_logger(__name__)



class WebSocketHITLController(HITLController):
    """
    WebSocket 模式下的 HITL 控制器。
    通过 WebSocket 向客户端发送审批请求，等待客户端回复。
    """

    def __init__(self, websocket, pending_approvals: dict[str, asyncio.Event] | None = None):
        self._ws = websocket
        self._pending_approvals: dict[str, asyncio.Event] = pending_approvals or {}
        self._approval_results: dict[str, bool] = {}

    async def request_approval(
        self,
        tool_name: str,
        reason: str,
        tool_call,
    ) -> bool:
        """通过 WebSocket 请求审批。"""
        call_id = tool_call.id
        event = asyncio.Event()
        self._pending_approvals[call_id] = event

        # 发送审批请求到客户端
        try:
            await self._ws.send(json.dumps({
                "type": "hitl_request",
                "tool_name": tool_name,
                "reason": reason,
                "args": tool_call.arguments,
                "call_id": call_id,
            }, ensure_ascii=False))
        except ConnectionClosed:
            return False

        # 等待客户端回复（带超时）
        try:
            await asyncio.wait_for(event.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            self._pending_approvals.pop(call_id, None)
            return False

        return self._approval_results.pop(call_id, False)

    def handle_hitl_response(self, call_id: str, approved: bool) -> None:
        """处理客户端发来的 HITL 审批回复。"""
        self._approval_results[call_id] = approved
        event = self._pending_approvals.pop(call_id, None)
        if event:
            event.set()


def _build_agent(
    config_path: str,
    websocket,
    hitl_controller: WebSocketHITLController,
    session_id: str | None = None,
    state_store = None,
) -> Agent:
    """
    根据配置文件构建完整的 Agent 实例。
    复用与 CLI 相同的构建逻辑。
    """
    raw = load_yaml_config(config_path)
    app_config = raw.get("agent", raw) if raw else {}
    config_obj = AgentConfig(**app_config)

    # ── 构建 Provider ──
    providers = []
    for p_cfg in config_obj.providers:
        if p_cfg.type.lower() == "openai":
            providers.append(OpenAIProvider(
                name=p_cfg.name,
                model=p_cfg.model,
                api_key=p_cfg.api_key or "sk-dummy",
                api_base=p_cfg.api_base,
            ))
        elif p_cfg.type.lower() == "anthropic":
            providers.append(AnthropicProvider(
                name=p_cfg.name,
                model=p_cfg.model,
                api_key=p_cfg.api_key or "sk-dummy",
            ))

    if not providers:
        raise RuntimeError("未配置任何 Provider，请检查 config.yaml")

    router = ProviderRouter(providers)

    # ── 准备 Hooks ──
    hooks = HookManager()

    async def _send(data: dict) -> None:
        """安全发送 JSON 消息到 WebSocket 客户端。"""
        try:
            await websocket.send(json.dumps(data, ensure_ascii=False))
        except ConnectionClosed:
            logger.warning("WebSocket connection closed while sending")
        except Exception as e:
            logger.error(f"WebSocket send error: {e}")

    @hooks.hook("stream")
    async def _on_stream(ctx, delta):
        await _send({"type": "text_delta", "text": delta})

    @hooks.hook("thinking_stream")
    async def _on_thinking_stream(ctx, delta):
        await _send({"type": "thinking_delta", "text": delta})

    @hooks.hook("stream_start")
    async def _on_stream_start(ctx):
        await _send({"type": "stream_start"})

    @hooks.hook("stream_end")
    async def _on_stream_end(ctx, resuming=False):
        await _send({"type": "stream_end", "resuming": resuming})

    @hooks.hook("tool_start")
    async def _on_tool_start(ctx, tool_name, args, call_id):
        await _send({
            "type": "tool_start",
            "tool_name": tool_name,
            "args": args,
            "call_id": call_id,
        })

    @hooks.hook("tool_end")
    async def _on_tool_end(ctx, tool_name, result, call_id, latency_ms):
        await _send({
            "type": "tool_end",
            "tool_name": tool_name,
            "result": result.content,
            "latency_ms": latency_ms,
            "call_id": call_id,
        })

    @hooks.hook("tool_error")
    async def _on_tool_error(ctx, tool_name, error, call_id):
        await _send({
            "type": "tool_error",
            "tool_name": tool_name,
            "error": str(error),
            "call_id": call_id,
        })

    @hooks.hook("safety_blocked")
    async def _on_safety_blocked(ctx, rule, reason, action, call_id="", tool_name=""):
        await _send({
            "type": "safety_blocked",
            "rule": rule,
            "reason": reason,
            "action": action,
            "call_id": call_id,
            "tool_name": tool_name,
        })

    @hooks.hook("state_change")
    async def _on_state_change(ctx, state):
        await _send({"type": "state_change", "state": state})

    @hooks.hook("error")
    async def _on_error(ctx, error):
        await _send({"type": "error", "message": str(error)})

    # 注册超时警告回调：看门狗发出 timeout_warning 时通过 WebSocket 推送给前端
    async def _on_timeout_warning(ctx, **kw):
        try:
            await websocket.send(json.dumps({
                "type": "timeout_warning",
                "stage": kw.get("stage", ""),
                "timeout_seconds": kw.get("timeout_seconds", 0),
                "message": kw.get("message", "操作超时"),
            }, ensure_ascii=False))
        except ConnectionClosed:
            pass
    hooks.on("timeout_warning", _on_timeout_warning)

    # 审计：直接传 AuditLogger 给 Agent（内联），不再通过 Hook 注册
    audit_logger = None
    if config_obj.audit.enabled:
        audit_dir = Path(config_obj.audit.jsonl_log_dir)
        audit_dir.mkdir(parents=True, exist_ok=True)
        jsonl_backend = JsonlAuditBackend(
            file_path=f"{config_obj.audit.jsonl_log_dir}/audit.jsonl"
        )
        audit_logger = AuditLogger(backend=jsonl_backend)

    # ── 系统提示词 ──
    sys_prompt = config_obj.system_prompt or "你是一个智能助手，可以帮助用户完成各种任务。"
    
    if config_obj.system_prompt_file:
        prompt_path = Path(config_obj.system_prompt_file)
        if prompt_path.exists():
            lines = []
            with open(prompt_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                        continue
                    lines.append(line.rstrip('\n'))
            sys_prompt = "\n".join(lines)
        else:
            print(f"Warning: system_prompt_file {config_obj.system_prompt_file} not found. Using fallback.")

    # ── 安全系统 ──
    safety_cfg = app_config.get("safety", {})
    safety_guard = None

    if safety_cfg.get("enabled", False):
        rules_path = safety_cfg.get("rules_path", "./config/safety_rules.yaml")
        rules_cfg = {}
        if Path(rules_path).exists():
            with open(rules_path) as f:
                import yaml
                rules_cfg = yaml.safe_load(f) or {}

        policy_cfg = rules_cfg.get("policy_engine", {})
        policy_engine = PolicyEngine(
            tool_policies=policy_cfg.get("tool_policies", []),
            default_action=policy_cfg.get(
                "default_action", safety_cfg.get("default_action", "allow")
            ),
        )

        cli_fence_cfg = rules_cfg.get("cli_fence", {})
        rules = [
            CLIFence(
                allowed_commands=cli_fence_cfg.get("allowed_commands"),
                denied_patterns=cli_fence_cfg.get("denied_patterns"),
                denied_paths=cli_fence_cfg.get("denied_paths"),
            ),
            InputContentFilter(),
            OutputContentFilter(),
        ]

        safety_guard = SafetyGuard(
            policy_engine=policy_engine,
            rules=rules,
        )
        logger.info("WebSocket: SafetyGuard enabled")

    # ── 沙盒 ──
    sandbox_cfg = app_config.get("sandbox", {})
    sandbox = SubprocessSandbox(
        limits=ResourceLimits(
            max_cpu_seconds=sandbox_cfg.get("max_cpu_seconds", 30),
            max_memory_mb=sandbox_cfg.get("max_memory_mb", 512),
            max_output_bytes=sandbox_cfg.get("max_output_bytes", 102400),
            timeout_seconds=sandbox_cfg.get("timeout_seconds", 60.0),
        )
    )

    # ── 密钥管理 ──
    secrets_cfg = app_config.get("secrets", {})
    secret_manager = SecretManager(
        env_prefix=secrets_cfg.get("env_prefix", "MYAGENT_SECRET_"),
        sensitive_fields=secrets_cfg.get("sensitive_fields"),
    )

    # ── 工具注册 ──
    tool_registry = ToolRegistry()
    tool_registry.register(CLITool(sandbox))
    tool_registry.register(FileReadTool())
    tool_registry.register(FileWriteTool())
    logger.info("WebSocket: Registered tools: cli_execute, file_read, file_write")

    # ── 构建 Agent（HookManager + 审计内联 + 超时配置）──
    agent = Agent(
        provider_router=router,
        hooks=hooks,
        tool_registry=tool_registry,
        system_prompt=sys_prompt,
        max_iterations=config_obj.max_iterations,
        safety_guard=safety_guard,
        secret_manager=secret_manager,
        hitl_callback=hitl_controller.request_approval,
        audit_logger=audit_logger,
        timeout_config=config_obj.timeout,
        state_store=state_store,
        session_id=session_id,
    )

    return agent


class WebSocketServer:
    """
    WebSocket 服务器。
    每个客户端连接维护独立的 Agent 实例和会话。
    支持多会话管理：创建、切换、列表、删除。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        config_path: str = "config.yaml",
    ):
        self.host = host
        self.port = port
        self.config_path = config_path
        self.ws_lock = WebSocketLock()
        self._agents: dict[str, Agent] = {}  # session_id -> Agent
        self._running_tasks: dict[str, asyncio.Task] = {}  # session_id -> running task
        # StateStore for session persistence (initialized on first use)
        self._state_store = None
        self._state_store_initialized = False

    async def _get_state_store(self):
        """Lazily initialize and return the StateStore."""
        if not self._state_store_initialized:
            from myagent.context.state import SQLiteStateStore
            self._state_store = SQLiteStateStore()
            await self._state_store.initialize()
            self._state_store_initialized = True
        return self._state_store

    async def _handle_connection(self, websocket) -> None:
        """处理单个 WebSocket 客户端连接。"""
        session_id = uuid4().hex[:16]
        logger.info(f"WebSocket client connected, session: {session_id}")

        # 发送连接确认
        await websocket.send(json.dumps({
            "type": "connected",
            "session_id": session_id,
        }))

        store = await self._get_state_store()

        # 创建 HITL 控制器
        hitl_controller = WebSocketHITLController(websocket)

        # 构建 Agent
        try:
            agent = _build_agent(
                self.config_path, websocket, hitl_controller, session_id, store
            )
        except Exception as e:
            logger.error(f"Failed to build agent: {e}")
            await websocket.send(json.dumps({
                "type": "error",
                "message": f"Agent 初始化失败: {e}",
            }))
            await websocket.close()
            return

        # Register agent for this session
        self._agents[session_id] = agent

        try:
            async for raw_message in websocket:
                try:
                    data = json.loads(raw_message)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "无效的 JSON 消息",
                    }))
                    continue

                msg_type = data.get("type", "")

                if msg_type == "chat":
                    asyncio.create_task(self._handle_chat(websocket, agent, hitl_controller, session_id, data))

                elif msg_type == "cancel":
                    await self._handle_cancel(websocket, session_id)

                elif msg_type == "hitl_response":
                    await self._handle_hitl_response(hitl_controller, data)

                elif msg_type == "session_list":
                    await self._handle_session_list(websocket)

                elif msg_type == "session_create":
                    result = await self._handle_session_create(websocket, hitl_controller, session_id)
                    if result:
                        session_id, agent = result

                elif msg_type == "session_switch":
                    result = await self._handle_session_switch(websocket, hitl_controller, session_id, data)
                    if result:
                        session_id, agent = result

                elif msg_type == "session_delete":
                    await self._handle_session_delete(websocket, data)

                elif msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))

                else:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": f"未知的消息类型: {msg_type}",
                    }))

        except ConnectionClosed:
            logger.info(f"WebSocket client disconnected, session: {session_id}")
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
        finally:
            self._running_tasks.pop(session_id, None)
            self.ws_lock.cleanup(session_id)

    async def _handle_chat(
        self,
        websocket,
        agent: Agent,
        hitl_controller: WebSocketHITLController,
        session_id: str,
        data: dict,
    ) -> None:
        """处理聊天消息。使用 asyncio.Task 支持取消。"""
        user_text = data.get("text", "").strip()
        if not user_text:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "消息内容不能为空",
            }))
            return

        # 获取会话锁，防止并发请求
        if self.ws_lock.get_lock(session_id).locked():
            await websocket.send(json.dumps({
                "type": "error",
                "message": "上一条消息正在处理中，请等待完成",
            }))
            return

        await self.ws_lock.acquire(session_id)
        try:
            # 用 Task 包装 agent.run，以支持 cancel
            task = asyncio.create_task(agent.run(user_text))
            self._running_tasks[session_id] = task

            try:
                response = await task
            except AgentCancelledError as e:
                logger.info(f"Agent run cancelled (session={session_id}): {e}")
                try:
                    await websocket.send(json.dumps({
                        "type": "message_end",
                        "text": f"操作已取消 — {e.reason.value}",
                        "stop_reason": f"cancelled:{e.reason.value}",
                    }))
                except ConnectionClosed:
                    pass
                return
            except asyncio.CancelledError:
                logger.info(f"Agent run task cancelled (session={session_id})")
                try:
                    await websocket.send(json.dumps({
                        "type": "message_end",
                        "text": "",
                        "stop_reason": "cancelled",
                    }))
                except ConnectionClosed:
                    pass
                return
            finally:
                self._running_tasks.pop(session_id, None)

            # 发送最终完成消息
            await websocket.send(json.dumps({
                "type": "message_end",
                "text": response,
                "stop_reason": "completed",
            }))
        except Exception as e:
            logger.error(f"Agent run error (session={session_id}): {e}")
            try:
                await websocket.send(json.dumps({
                    "type": "error",
                    "message": f"Agent 执行出错: {e}",
                }))
            except ConnectionClosed:
                pass
        finally:
            self.ws_lock.release(session_id)

    async def _handle_cancel(
        self,
        websocket,
        session_id: str,
    ) -> None:
        """处理取消请求：通过 CancellationToken 协作式取消 Agent。"""
        # 优先使用协作式取消（CancellationToken）
        agent = self._agents.get(session_id)
        if agent:
            agent.request_cancel(CancelReason.USER_CANCEL, "用户通过 WebSocket 取消")
            logger.info(f"Cancel requested via CancellationToken for session: {session_id}")

        # 回退：直接取消 Task（兼容旧逻辑）
        task = self._running_tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            logger.info(f"Cancel requested via task.cancel() for session: {session_id}")
        else:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "当前没有正在运行的任务",
            }))

    async def _handle_hitl_response(
        self,
        hitl_controller: WebSocketHITLController,
        data: dict,
    ) -> None:
        """处理 HITL 审批回复。"""
        call_id = data.get("call_id", "")
        approved = data.get("approved", False)
        hitl_controller.handle_hitl_response(call_id, approved)

    async def _handle_session_list(self, websocket) -> None:
        """处理会话列表请求。"""
        store = await self._get_state_store()
        sessions = await store.list_all_sessions()
        # 为每个会话获取第一条用户消息作为标题
        for s in sessions:
            try:
                messages = await store.load_messages(s["session_id"])
                first_user = next((m for m in messages if m.role == "user"), None)
                content = ""
                if first_user and hasattr(first_user, 'content') and first_user.content:
                    if isinstance(first_user.content, str):
                        content = first_user.content
                    elif isinstance(first_user.content, list):
                        content = "".join((b.get('text') or "") if isinstance(b, dict) else (getattr(b, 'text', None) or "") for b in first_user.content if (b.get('type') if isinstance(b, dict) else getattr(b, 'type', '')) == 'text')
                    else:
                        content = str(first_user.content)
                s["title"] = content[:50] if content else "新对话"
                s["message_count"] = len(messages)
            except Exception:
                s["title"] = "新对话"
                s["message_count"] = 0
        await websocket.send(json.dumps({
            "type": "session_list_result",
            "sessions": sessions,
        }, ensure_ascii=False))

    async def _handle_session_create(self, websocket, hitl_controller, current_session_id: str) -> tuple[str, Agent] | None:
        """处理创建新会话请求。"""
        new_session_id = uuid4().hex[:16]
        store = await self._get_state_store()
        # Build a new agent for this session
        try:
            agent = _build_agent(
                self.config_path, websocket, hitl_controller, new_session_id, store
            )
        except Exception as e:
            await websocket.send(json.dumps({
                "type": "error",
                "message": f"创建会话失败: {e}",
            }))
            return

        # Cancel any running task for old session
        old_task = self._running_tasks.pop(current_session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        # Register new session
        self._agents[new_session_id] = agent
        self.ws_lock.cleanup(current_session_id)

        await websocket.send(json.dumps({
            "type": "session_created",
            "session_id": new_session_id,
        }))
        
        return new_session_id, agent

    async def _handle_session_switch(self, websocket, hitl_controller, current_session_id: str, data: dict) -> tuple[str, Agent] | None:
        """处理切换会话请求。"""
        target_id = data.get("session_id", "")
        if not target_id:
            await websocket.send(json.dumps({"type": "error", "message": "缺少 session_id"}))
            return

        store = await self._get_state_store()

        # Build new agent for target session
        try:
            agent = _build_agent(
                self.config_path, websocket, hitl_controller, target_id, store
            )
            await agent.restore_session(target_id)
        except Exception as e:
            await websocket.send(json.dumps({"type": "error", "message": f"切换会话失败: {e}"}))
            return

        # Cancel running task for old session
        old_task = self._running_tasks.pop(current_session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        # Register
        self._agents[target_id] = agent
        self.ws_lock.cleanup(current_session_id)

        # Load history messages
        messages = await store.load_messages(target_id)
        history = []
        for msg in messages:
            entry = {"role": msg.role, "content": ""}
            if hasattr(msg, 'content') and msg.content:
                if isinstance(msg.content, str):
                    entry["content"] = msg.content
                elif isinstance(msg.content, list):
                    entry["content"] = "".join((b.get('text') or "") if isinstance(b, dict) else (getattr(b, 'text', None) or "") for b in msg.content if (b.get('type') if isinstance(b, dict) else getattr(b, 'type', '')) == 'text')
                else:
                    entry["content"] = str(msg.content)
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": getattr(tc, 'id', None) if not isinstance(tc, dict) else tc.get('id'),
                        "name": getattr(tc, 'name', None) if not isinstance(tc, dict) else tc.get('name'),
                        "arguments": getattr(tc, 'arguments', {}) if not isinstance(tc, dict) else tc.get('arguments', {}),
                    }
                    for tc in msg.tool_calls
                ]
            if hasattr(msg, 'tool_call_id') and msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            if hasattr(msg, 'tool_name') and msg.tool_name:
                entry["tool_name"] = msg.tool_name
            if hasattr(msg, 'metadata') and msg.metadata:
                entry["metadata"] = msg.metadata
            history.append(entry)

        await websocket.send(json.dumps({
            "type": "session_switched",
            "session_id": target_id,
            "messages": history,
        }, ensure_ascii=False))

        return target_id, agent

    async def _handle_session_delete(self, websocket, data: dict) -> None:
        """处理删除会话请求。"""
        target_id = data.get("session_id", "")
        if not target_id:
            await websocket.send(json.dumps({"type": "error", "message": "缺少 session_id"}))
            return

        store = await self._get_state_store()
        await store.clear_session(target_id)
        self._agents.pop(target_id, None)
        self._running_tasks.pop(target_id, None)

        await websocket.send(json.dumps({"type": "session_deleted", "session_id": target_id}))

    async def start_async(self) -> None:
        """异步启动 WebSocket 服务器。"""
        logger.info(f"WebSocket Server starting on ws://{self.host}:{self.port}")
        async with websockets.serve(
            self._handle_connection,
            self.host,
            self.port,
            ping_interval=30,
            ping_timeout=10,
            max_size=10 * 1024 * 1024,  # 10MB max message size
        ):
            await asyncio.Future()  # 永久运行

    def start(self) -> None:
        """启动 WebSocket 服务器（阻塞）。"""
        asyncio.run(self.start_async())


def main():
    """CLI 入口：python -m myagent.interfaces.websocket.server"""
    import argparse

    parser = argparse.ArgumentParser(description="MyAgent WebSocket Server")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    server = WebSocketServer(
        host=args.host,
        port=args.port,
        config_path=args.config,
    )
    server.start()


if __name__ == "__main__":
    main()