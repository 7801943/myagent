class AgentHarness:
    """
    Agent 核心调度器 (Harness)
    
    设计理念：
    1. 不包含任何特定业务逻辑（如具体的 prompt 是什么）。
    2. 只负责标准 ReAct 循环或 Orchestrator 驱动的循环。
    3. 关键节点均派发 Event，允许外部灵活扩展行为。
    """

    def __init__(
        self,
        event_bus: IEventBus,
        session_manager: ISessionManager,
        llm_provider: ILLMProvider,
        tool_registry: IToolRegistry,
        orchestrator: Optional[IOrchestrator] = None
    ):
        """
        依赖注入 (Dependency Injection)：
        Harness 被实例化时，外部框架必须将实现了对应 Protocol 的实例注入进来。
        这极大地方便了 Mock 测试和底层切换。
        """
        self.bus = event_bus
        self.session = session_manager
        self.llm = llm_provider
        self.tools = tool_registry
        self.orchestrator = orchestrator

    async def run(self, session_id: str, user_input: UserInput) -> None:
        """
        [主入口] 接收用户输入并驱动 Agent 运转。
        """
        # 1. 初始化运行上下文
        context = HarnessContext(session_id=session_id)
        
        # 2. 触发会话开始事件
        # self.bus.publish(AgentRunStartedEvent(trace_id=context.trace_id, user_input=user_input))

        # 3. 将用户输入持久化到 Session Memory
        # await self.session.append_memory(session_id, ...)

        try:
            # 4. 进入状态机 / 思考-行动循环 (Thought-Action Loop)
            await self._loop(context)
            
        except Exception as e:
            # self.bus.publish(AgentErrorEvent(...))
            raise
        finally:
            # self.bus.publish(AgentRunFinishedEvent(...))
            pass

    async def _loop(self, context: HarnessContext) -> None:
        """
        [核心引擎] 控制 Agent 的运转周期，直到达成目标或触及限制。
        """
        max_iterations = 15
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            
            # 步骤 A：规划/准备 (如果有 Orchestrator，则由其决定上下文；否则直连 LLM)
            # 步骤 B：执行大脑推理
            is_finished, tool_requests = await self._step_inference(context)
            
            if is_finished:
                break # LLM 决定结束当前会话
                
            if tool_requests:
                # 步骤 C：执行动作 (并行或串行)
                await self._step_execute_tools(context, tool_requests)

    async def _step_inference(self, context: HarnessContext) -> tuple[bool, List[ToolExecuteRequest]]:
        """
        执行大脑推理步骤。
        返回: (是否需要退出循环, 发起的工具调用列表)
        """
        # 1. 获取历史记录与工具 Schema
        # 2. 组装 LLMRequest
        # 3. 发送给 LLM 并处理流式返回 (此过程中持续往外 publish 文本块 Event)
        # 4. 判定返回结果是纯文本，还是 ToolCall
        pass

    async def _step_execute_tools(self, context: HarnessContext, requests: List[ToolExecuteRequest]) -> None:
        """
        执行工具步骤。
        """
        # 1. 触发工具开始执行 Event
        # 2. 批量交给 IToolRegistry.execute(req)
        # 3. 将 ToolExecuteResult 存回 Session Memory
        # 4. 触发工具执行完毕 Event
        pass