"""
Session：一等公民 Web 会话容器。

瘦会话层：状态容器 + 转发 + 持久化。
WS 多客户端管理和审批桥接委托给 ClientBridge。
Harness 为 per-session 独占实例。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Awaitable
from uuid import uuid4

from myagent.context.manager import ContextManager
from myagent.core.session.client_bridge import ClientBridge, ClientHandle
from myagent.core.events import EventHandle, StateChange, ToolEnd
from myagent.core.harness import AgentHarness
from myagent.core.session.serializer import serialize_messages
from myagent.core.models import UserContext, SessionData, SessionState, AgentRunState
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.core.workspace import WorkspaceManager, WorkspaceState
    from myagent.context.state import StateStore
    from myagent.prompt.renderer import PromptRenderer
    from myagent.prompt.skills import SkillRegistry

logger = get_logger(__name__)


class Session:
    """
    一等公民 Web 会话容器。

    持有 AgentHarness 引用（调度中枢），通过 harness.run() 执行交互。
    WS 多客户端管理和审批桥接委托给 ClientBridge。
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        harness: AgentHarness,
        user: UserContext,
        state_store: "StateStore | None" = None,
        system_prompt: str | None = None,
        max_tokens_budget: int = 200000,
        context_window_size: int = 200000,
        tool_result_max_chars: int = 200000,
        workspace_root: str | None = None,
        workspace_resolver=None,
        hitl_enabled: bool = True,
        approval_timeout: float = 300.0,
        skill_registry: "SkillRegistry | None" = None,
        name: str | None = "新会话",
    ):
        self.id: str = session_id or uuid4().hex[:16]
        self.created_at: datetime = datetime.now(timezone.utc)
        self.user = user
        self.last_active_at: datetime = datetime.now(timezone.utc)
        self.name = name

        # ── 唯一状态容器 ──
        self.data = SessionData(
            user={"user_id": user.user_id, "username": user.username},
            context={
                "token_usage": {"used": 0, "total": context_window_size},
                "agent_run_state": AgentRunState.IDLE.value,
                "session_state": SessionState.ACTIVE.value,
                "stop_reason": "",
                "cancelled": False,
            },
        )

        # 共享 Harness 引用
        self._harness = harness
        self._state_store = state_store

        # Per-session ContextManager（带实时持久化）
        self._context = ContextManager(
            max_tokens_budget=max_tokens_budget,
            context_window_size=context_window_size,
            tool_result_max_chars=tool_result_max_chars,
            state_store=state_store,
            session_id=self.id,
        )

        self._running_task: asyncio.Task | None = None
        self._cancel_reason: str = ""
        self._cancel_detail: str = ""

        # WS 多客户端管理 + 审批桥接（委托给 ClientBridge）
        self._bridge = ClientBridge(
            harness.events,
            self.id,
            approval_timeout=approval_timeout,
        )

        # 审批回调：默认使用 ClientBridge（Web 场景），CLI 可覆盖
        self._approval_handler: Callable[[list], Awaitable[list[bool]]] | None = (
            self._bridge.approval_handler if hitl_enabled else None
        )

        # PromptRenderer（由 SessionManager 注入）
        self._prompt_renderer: "PromptRenderer | None" = None
        self._skill_registry = skill_registry

        # WorkspaceManager
        self.workspace: "WorkspaceManager | None" = None
        self.workspace_resolver = workspace_resolver
        if workspace_root or workspace_resolver:
            from myagent.core.workspace import WorkspaceManager
            self.workspace = WorkspaceManager(
                workspace_root or getattr(workspace_resolver, "virtual_root", ""),
                resolver=workspace_resolver,
            )
            self.workspace.set_on_change(self._on_workspace_change)

        if system_prompt:
            self._context.set_system(system_prompt)

        # ── 从 Harness 采集初始状态 ──
        self._init_meta_from_harness()
        self._sync_safety_policy_state()

        # 注册状态同步事件
        self._event_handles: list[EventHandle] = []
        self._event_handles.append(
            harness.events.on(StateChange, self._on_state_change, topic=self.id)
        )
        self._event_handles.append(
            harness.events.on(ToolEnd, self._on_tool_end, topic=self.id)
        )

        # ── 内置并发锁 ──
        self._chat_lock = asyncio.Lock()

        # 系统指令处理器
        self._system_command_handler = None

    # ── 事件取消注册 ──

    def unregister_events(self) -> None:
        """取消注册所有事件回调（Session 销毁时调用）。"""
        for handle in self._event_handles:
            handle.unregister()
        self._event_handles.clear()

    def unregister_hooks(self) -> None:
        """Backward-compatible alias for unregister_events()."""
        self.unregister_events()

    # ── 空会话判定 ──

    def has_user_message(self) -> bool:
        """判断会话中是否存在用户消息（用于空会话过滤）。"""
        return any(m.role == "user" for m in self._context.messages)

    def _should_persist_session_state(self) -> bool:
        """只有真实用户输入后的会话才落库。

        登录后自动创建的空会话会产生 workspace/client_state 变化，但这些
        前端查看、编辑状态只应保留在内存中，避免空对话出现在会话列表。
        """
        return self.has_user_message()

    async def _persist_workspace_if_allowed(self) -> None:
        if not self.workspace or not self._state_store:
            return
        if not self._should_persist_session_state():
            logger.debug(
                "Workspace state kept in memory for empty session: %s",
                self.id,
            )
            return
        ws_json = json.dumps(self.workspace.snapshot().to_dict(), ensure_ascii=False)
        await self._state_store.save_workspace(self.id, ws_json)

    # ── 兼容属性 ──

    @property
    def session_state(self) -> SessionState:
        try:
            return SessionState(self.data.context.session_state)
        except ValueError:
            return SessionState.ACTIVE

    @session_state.setter
    def session_state(self, value: SessionState) -> None:
        self.data.context.session_state = value.value

    @property
    def agent_run_state(self) -> AgentRunState:
        try:
            return AgentRunState(self.data.context.agent_run_state)
        except ValueError:
            return AgentRunState.IDLE

    @agent_run_state.setter
    def agent_run_state(self, value: AgentRunState) -> None:
        self.data.context.agent_run_state = value.value

    @property
    def metadata(self) -> dict:
        return self.data.model_dump()

    @property
    def context(self) -> ContextManager:
        return self._context

    @property
    def harness(self) -> AgentHarness:
        """公开 getter：获取关联的 AgentHarness 实例。"""
        return self._harness

    # ── 从 Harness 初始化 meta ──

    def _init_meta_from_harness(self) -> None:
        """从 AgentHarness 采集初始状态到 data（模型列表、工具列表等）。"""
        router = self._harness.router

        # 采集模型信息
        self._sync_model_state_from_router()

        # 采集工具列表
        tools: list[dict] = []
        ti = self._harness.tool_interface
        if ti:
            for record in ti.list_schemas() or []:
                tools.append({
                    "name": record.get("name", ""),
                    "description": record.get("description", ""),
                    "parameters_schema": record.get("parameters_schema", {}),
                    "source": record.get("source", ""),
                })
        self.data.tool.tools = tools

    def _sync_model_state_from_router(self) -> None:
        """同步 router/provider 状态到 SessionData.model。"""
        router = self._harness.router
        selected_key = getattr(router, "selected_provider_key", "")
        available_models: list[dict] = []
        active_model: dict = {}

        for p in router.providers:
            ptype = type(p).__name__.replace("Provider", "").lower()
            cws = getattr(p, "_context_window_size", 128000)
            provider_key = (
                router.provider_key(p)
                if hasattr(router, "provider_key")
                else p.name
            )
            is_current = provider_key == selected_key
            info = {
                "provider_key": provider_key,
                "provider_name": p.name,
                "model_id": p.model,
                "provider_type": ptype,
                "context_window_size": cws,
                "thinking_supported": bool(getattr(p, "thinking_supported", False)),
                "thinking_enabled": bool(getattr(p, "thinking_enabled", False)),
                "is_active": is_current,
            }
            available_models.append(info)
            if is_current:
                active_model = info

        if not active_model.get("provider_name") and available_models:
            active_model = available_models[0]
            active_model["is_active"] = True

        self.data.model.active = active_model
        self.data.model.available = available_models

        active_cws = active_model.get("context_window_size", 200000)
        self.data.context.token_usage.total = active_cws
        if hasattr(self._context, "_context_window_size"):
            self._context._context_window_size = active_cws

    # ── 会话级安全策略 ──

    def _sync_safety_policy_state(self) -> dict:
        state = self._harness.tool_interface.get_cli_policy_state()
        self.data.safety.active_policy = state["active_policy"]
        self.data.safety.available_policies = state["available_policies"]
        self.data.safety.mode = state["mode"]
        self.data.agent.safety_enabled = (
            getattr(self._harness, "safety_guard", None) is not None
        )
        return state

    def _apply_safety_policy(self, policy_name: str) -> dict:
        state = self._harness.tool_interface.set_cli_policy(policy_name)
        self.data.safety.active_policy = state["active_policy"]
        self.data.safety.available_policies = state["available_policies"]
        self.data.safety.mode = state["mode"]
        return state

    async def set_safety_policy(
        self,
        policy_name: str,
        *,
        allow_while_running: bool = False,
    ) -> dict:
        if self._chat_lock.locked() and not allow_while_running:
            raise RuntimeError("Session is busy; safety policy cannot be changed")
        state = self._apply_safety_policy(policy_name)
        await self._persist_state(self.agent_run_state)
        await self.push_conversation_state()
        return state

    async def set_model_selection(
        self,
        provider_key: str,
        *,
        thinking_enabled: bool | None = None,
        allow_while_running: bool = False,
    ) -> dict:
        if self._chat_lock.locked() and not allow_while_running:
            raise RuntimeError("Session is busy; model cannot be changed")
        router = self._harness.router
        provider = router.set_provider(provider_key)
        if thinking_enabled is not None:
            if thinking_enabled and not getattr(provider, "thinking_supported", False):
                raise ValueError("当前模型不支持 Thinking 开关")
            provider.thinking_enabled = bool(
                thinking_enabled and getattr(provider, "thinking_supported", False)
            )
        self._sync_model_state_from_router()
        await self._persist_state(self.agent_run_state)
        await self.push_conversation_state()
        return self.data.model.model_dump()

    # ── PromptRenderer 注入 ──

    def set_prompt_renderer(self, renderer: "PromptRenderer") -> None:
        self._prompt_renderer = renderer

    # ── 工具统一管理 ──

    async def update_tools(self, source: str = "agent") -> None:
        """从 ToolManager 重新采集工具列表，同步到 meta。"""
        from myagent.prompt.variables import _summarize_parameters

        tools: list[dict] = []
        ti = self._harness.tool_interface
        if ti:
            for record in ti.list_schemas() or []:
                params_schema = record.get("parameters_schema", {})
                tools.append({
                    "name": record.get("name", ""),
                    "description": record.get("description", ""),
                    "parameters_schema": params_schema,
                    "parameters_summary": _summarize_parameters(
                        params_schema
                    ),
                    "source": record.get("source", ""),
                    "category": record.get("category", ""),
                })
        self.data.tool.tools = tools

    # ── 事件回调 ──

    async def _on_state_change(self, event: StateChange) -> None:
        self.data.context.agent_run_state = event.state

    async def _on_tool_end(self, event: ToolEnd) -> None:
        if not self.workspace:
            return
        tool_name = event.tool_name
        result = event.result
        file_tools = {"file_write", "file_read", "file_query", "file_diff", "file_edit", "file_edit_table", "cli_execute"}
        if tool_name in file_tools:
            await self.workspace.update("agent", "files_changed", {})

        # 读/写/编辑工具成功操作文件后，把对应文件设为 active tab。
        # 这让前端在 agent 操作完成后自动预览或刷新 OnlyOffice 编辑器。
        if tool_name in {"file_read", "file_query", "file_write", "file_edit", "file_edit_table"} and hasattr(result, 'metadata'):
            if getattr(result, "is_error", False):
                return
            file_path = result.metadata.get("path", "") if isinstance(result.metadata, dict) else ""
            if file_path:
                import os
                resolver = getattr(self.workspace, "resolver", None)
                rel_path = resolver.to_virtual_path(file_path) if resolver else None
                root = self.workspace.root_path
                if not rel_path and file_path.startswith(root):
                    rel_path = os.path.relpath(file_path, root)
                if not rel_path:
                    rel_path = file_path
                await self.workspace.update("agent", "open_file", {"path": rel_path})

    # ── 客户端状态同步 / 动态上下文 ──

    async def apply_client_state(self, client_state: dict | None) -> None:
        """合并前端运行态，供本轮和后续动态 prompt 使用。"""
        if not isinstance(client_state, dict):
            return

        workspace_state = client_state.get("workspace")
        model_state = client_state.get("model")
        tools_state = client_state.get("tools")

        if isinstance(workspace_state, dict):
            self.data.client_state.workspace = workspace_state
            if self.workspace:
                await self.workspace.update("user", "sync_client_state", workspace_state)
        if isinstance(model_state, dict):
            self.data.client_state.model = model_state
        if isinstance(tools_state, dict):
            self.data.client_state.tools = tools_state

        extra_state = {
            k: v
            for k, v in client_state.items()
            if k not in {"workspace", "model", "tools"}
        }
        if extra_state:
            self.data.client_state.extra.update(extra_state)

        logger.debug(
            "Client state applied: session=%s workspace=%s model=%s tools=%s",
            self.id,
            isinstance(workspace_state, dict),
            isinstance(model_state, dict),
            isinstance(tools_state, dict),
        )

    async def refresh_dynamic_context(self) -> None:
        """重新渲染动态 system prompt，让 LLM 调用前看到最新会话状态。"""
        if not self._prompt_renderer:
            return
        from myagent.prompt.variables import VariableCollector
        variables = await VariableCollector.collect(self)
        rendered_prompt = self._prompt_renderer.render(variables)
        self._context.set_system(rendered_prompt)

    # ── 核心对话 ──

    async def chat(self, user_input: str | list, client_state: dict | None = None) -> str:
        """发起一轮对话。内置并发锁。"""
        if not self._chat_lock.locked():
            async with self._chat_lock:
                return await self._chat_inner(user_input, client_state=client_state)
        else:
            raise RuntimeError("Session is busy, please wait")

    async def _chat_inner(self, user_input: str | list, client_state: dict | None = None) -> str:
        """
        chat 的实际实现（在锁内执行）。

        职责：
          1. 渲染 System Prompt
          2. 写入用户消息
          3. 转发到 harness.run()
          4. 状态持久化

        result.text 已被 Harness 内部的 finalize_content 处理过。
        """
        self.last_active_at = datetime.now(timezone.utc)
        self._running_task = asyncio.current_task()
        self._cancel_reason = ""
        self._cancel_detail = ""

        logger.info(f"Session chat start: {self.id}")

        # 重置 per-turn token 累计 + 开始计时
        self._context.reset_turn_usage()
        turn_start_time = time.monotonic()

        try:
            await self.apply_client_state(client_state)

            # 1. 渲染动态 system prompt（SSPT）
            await self.refresh_dynamic_context()

            # 2. 写入用户消息
            if isinstance(user_input, list):
                await self._context.add_user_message(user_input)
            elif user_input:
                await self._context.add_user_message(user_input)

            # 3. 调用无状态执行引擎
            result = await self._harness.run(
                context=self._context,
                session_id=self.id,
                session_data=self.data,
                command_handler=self._system_command_handler,
                approval_handler=self._approval_handler,
                dynamic_context_handler=self.refresh_dynamic_context,
            )

            logger.info(f"Session chat end: {self.id}, reason={result.stop_reason}")

            # 3.5 将本轮耗时和 token 消耗写入最后一条 assistant 消息的 metadata（持久化，历史会话也能显示）
            elapsed_ms = int((time.monotonic() - turn_start_time) * 1000)
            await self._context.add_turn_metadata({
                "elapsed_ms": elapsed_ms,
                "input_tokens": self._context.turn_input_tokens,
                "output_tokens": self._context.turn_output_tokens,
            })

            # 4. 状态持久化
            self.data.context.stop_reason = result.stop_reason or "completed"
            await self._persist_state(AgentRunState.IDLE)

            return result.text or ""

        except asyncio.CancelledError:
            reason = self._cancel_reason or "user_cancelled"
            cancel_msg = f"[系统] 操作已取消 — {reason}"
            if self._cancel_detail:
                cancel_msg += f": {self._cancel_detail}"
            logger.info(f"Session chat cancelled (session-level): {reason}")
            try:
                self.data.context.cancelled = True
                self.data.extra["cancel_reason"] = reason
                await asyncio.shield(self._persist_state(AgentRunState.IDLE))
            except Exception:
                pass
            return cancel_msg

        except Exception as e:
            logger.error(f"Session chat error: {e}", exc_info=True)
            await self._persist_state(AgentRunState.ERROR)
            raise

        finally:
            self._running_task = None

    def request_cancel(
        self,
        reason: str = "user_cancelled",
        detail: str = "",
    ) -> None:
        """供外部（CLI/WebSocket）调用的取消入口。"""
        self._cancel_reason = reason
        self._cancel_detail = detail
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()
            logger.info(f"Session cancel requested: {reason} — {detail}")

    def update_metadata(self, key: str, value) -> None:
        self.data.extra[key] = value

    # ── 持久化 ──

    async def _persist_state(self, state: AgentRunState | None = None):
        """Session 负责的 SessionData 状态持久化（消息由 ContextManager 自动持久化）。

        空会话允许在内存中维护 workspace/client_state，但不能创建数据库行。
        """
        if state is not None:
            self.data.context.agent_run_state = state.value
        self.data.context.token_usage.used = self._context.last_usage_input_tokens
        if self.workspace:
            self.data.workspace.state = self.workspace.snapshot().to_dict()
        if not self._should_persist_session_state():
            logger.debug("Session state kept in memory for empty session: %s", self.id)
            return
        await self._persist_workspace_if_allowed()
        if self._state_store:
            await self._state_store.save_state(
                self.id,
                self.agent_run_state,
                self.data.model_dump(),
                self.session_state,
            )

    async def save(self) -> None:
        """TTL 驱逐 / WS 断开时调用：状态持久化 + 强制刷消息。"""
        await self._persist_state(self.agent_run_state)
        if self._state_store:
            await self._context.flush()

    async def load_messages(self) -> list:
        if not self._state_store:
            return []
        return await self._state_store.load_messages(self.id)

    def make_command_handler(self):
        """创建系统指令处理器并保存为实例属性。"""
        session = self

        async def _system_command_handler(cmd: str, args: str, ctx) -> None:
            if cmd == "new":
                logger.info(f"System command: /new — clearing context for session {session.id}")
                session._context.clear()
            elif cmd == "model":
                provider_name = args.strip()
                if provider_name and hasattr(session._harness, 'router'):
                    try:
                        await session.set_model_selection(
                            provider_name,
                            allow_while_running=True,
                        )
                        logger.info(f"System command: /model → {provider_name}")
                    except Exception as e:
                        logger.warning(f"Failed to switch model: {e}")
            elif cmd == "safety":
                policy_name = args.strip()
                if policy_name:
                    try:
                        await session.set_safety_policy(
                            policy_name,
                            allow_while_running=True,
                        )
                        logger.info(f"System command: /safety → {policy_name}")
                    except Exception as e:
                        logger.warning(f"Failed to switch safety policy: {e}")
            else:
                logger.debug(f"Unknown system command: /{cmd} {args}")

        self._system_command_handler = _system_command_handler

    # ── Workspace 回调 ──

    async def _on_workspace_change(self, state: "WorkspaceState", source: str) -> None:
        if self._state_store and self._should_persist_session_state():
            ws_json = json.dumps(state.to_dict(), ensure_ascii=False)
            await self._state_store.save_workspace(self.id, ws_json)
        elif self._state_store:
            logger.debug(
                "Workspace change from %s kept in memory for empty session: %s",
                source,
                self.id,
            )
        # workspace_state 是前端文件树与 OnlyOffice 编辑器的单一同步源。
        # user/agent 两类更新都广播，便于多客户端和刷新恢复保持一致。
        await self._bridge.notify_clients("workspace_state", state.to_dict())

    # ── 委托 ClientBridge ──

    def attach_client(self, sender) -> ClientHandle:
        """将一个 WS 客户端接入本会话。委托给 ClientBridge。"""
        return self._bridge.attach_client(sender)

    # ── 消息序列化 ──

    def serialize_messages(self) -> list[dict]:
        return serialize_messages(self._context.messages)

    # ── 前端推送 ──

    async def push_conversation_state(self) -> None:
        if self._bridge.has_clients:
            await self._bridge.notify_clients("conversation_state", self.data.model_dump())
