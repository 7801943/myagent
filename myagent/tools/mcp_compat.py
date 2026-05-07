"""
MCP (Model Context Protocol) 兼容层。
当前为接口预留，未来实现时只需填充此文件，不影响其他模块。

兼容原理：
  MyAgent 的 BaseTool.parameters_schema 与 MCP inputSchema 格式一致（标准 JSON Schema），
  因此 MCP 工具可以直接包装为 BaseTool，走统一的 Registry/Executor 流水线。

  MCP 工具的元数据同样通过 config/tool_meta.yaml 配置，无需硬编码。

渐进式迁移路径：
  Phase 1（当前）: 自研 schema + ProcessRunner 本地子进程
  Phase 2: MCPTool 骨架 + MCPClientManager 接口预留
  Phase 3: 实现 MCPClientManager（stdio/SSE/HTTP 连接）
  Phase 4: 完整 MCP Server 模式（工具发现 + 远程调用 + 连接管理）
"""
from myagent.tools.base import BaseTool, ToolResult, ToolMeta


class MCPTool(BaseTool):
    """
    将 MCP Server 工具适配为 BaseTool。（预留骨架）

    MCP 工具的 inputSchema 与 BaseTool.parameters_schema 格式一致（标准 JSON Schema），
    因此可以零转换直接使用。

    元数据通过 config/tool_meta.yaml 加载，MCP 工具在配置中 source=mcp。

    用法（未来实现后）：
        tool = MCPTool(
            server_name="github",
            tool_name="create_issue",
            description="Create a GitHub issue",
            input_schema={"type": "object", "properties": {...}},
        )
        result = await tool.execute(title="Bug", body="...")
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict,
    ):
        self.name = f"mcp_{server_name}_{tool_name}"
        self.description = f"[MCP:{server_name}] {description}"
        self.parameters_schema = input_schema  # 格式完全一致，零转换
        # 元数据从配置文件加载，MCP 工具在配置中 source=mcp
        self.meta = ToolMeta.load(self.name)
        self._server_name = server_name
        self._tool_name = tool_name

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行 MCP 工具调用。

        TODO: 实现 MCP tool call（通过 MCP Client 发送请求）
        当前 MCP 协议使用 JSON-RPC 2.0，与 ProcessRunner 的通信协议兼容。
        实现时只需：
          1. 通过 MCPClientManager 发送 tools/call 请求
          2. 将 MCP 响应转换为 ToolResult
        """
        raise NotImplementedError(
            "MCP tool execution not yet implemented. "
            "See mcp_compat.py for integration plan."
        )


class MCPClientManager:
    """
    MCP Client 连接管理器。（预留骨架）

    未来实现时负责：
      - 管理 MCP Server 连接（stdio / SSE / HTTP）
      - 发现远程工具（tools/list）
      - 转发工具调用（tools/call）
      - 连接生命周期管理（心跳、重连）

    用法（未来实现后）：
        manager = MCPClientManager()
        await manager.connect("github", transport="stdio")
        tools = await manager.list_tools("github")
        result = await manager.call_tool("github", "create_issue", {...})
        await manager.disconnect("github")
    """

    async def connect(self, server_url: str, transport: str = "stdio") -> None:
        """
        连接 MCP Server。

        Args:
            server_url: 服务器地址（stdio 模式下为命令路径，HTTP 模式下为 URL）
            transport: 传输协议，支持 "stdio" | "sse" | "http"
        """
        raise NotImplementedError("MCP client not yet implemented")

    async def list_tools(self, server_name: str) -> list[MCPTool]:
        """
        发现远程工具，返回 MCPTool 列表。

        对应 MCP 协议的 tools/list 方法。
        """
        raise NotImplementedError("MCP client not yet implemented")

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict
    ) -> ToolResult:
        """
        调用远程工具，返回 ToolResult。

        对应 MCP 协议的 tools/call 方法。
        """
        raise NotImplementedError("MCP client not yet implemented")

    async def disconnect(self, server_name: str) -> None:
        """断开连接。"""
        raise NotImplementedError("MCP client not yet implemented")

    async def disconnect_all(self) -> None:
        """断开所有连接。"""
        raise NotImplementedError("MCP client not yet implemented")