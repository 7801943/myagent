import re

with open("phase2实施方案.md", "r", encoding="utf-8") as f:
    content = f.read()

# 1. 替换范围和不含
content = content.replace(
    "> **范围**：Phase 2 聚焦于构建**工具执行沙盒、安全策略引擎、HITL 审批机制、密钥管理器、多模态图像处理**——使框架具备生产级安全能力和实用工具集。\n> **不含**：SubAgent 系统（Phase 3）、Skill 系统/文档处理/WebSocket Server（Phase 4）、评测引擎/RAG（Phase 5）。",
    "> **范围**：Phase 2 聚焦于构建**工具执行沙盒、安全策略引擎、HITL 审批机制、密钥管理器、多模态图像处理**，并**提取原Phase 4的WebSocket Server和Web交互前端**到本阶段实现——使框架具备生产级安全能力、实用工具集和直观的可视化体验。\n> **不含**：SubAgent 系统（Phase 3）、Skill 系统/文档处理（Phase 4）、评测引擎/RAG（Phase 5）。"
)

# 2. 增加验收标准
content = content.replace(
    "# 7. 审计日志覆盖",
    "# 7. WebSocket 交互与前端 UI\n# 独立启动 websocket Server (python -m myagent.interfaces.websocket.server)\n# 通过浏览器打开 web/index.html，可与 Agent 进行全双工流式交互并渲染 Markdown 及工具调用结果。\n\n# 8. 审计日志覆盖"
)

# 3. 增强依赖图
old_dep = """                          ┌─────────────────────────┐
                          │ ⑧ CLI 增强 + 集成测试     │
                          │ interfaces/cli/main.py  │
                          └───────────┬─────────────┘"""
new_dep = """                          ┌──────────────────────────────────────────────┐
                          │ ⑧ CLI 增强, 集成测试, WebSocket与Web前端UI实现 │
                          │ interfaces/cli/main.py, interfaces/websocket │
                          │ web/ (index.html, js, css)                   │
                          └───────────┬──────────────────────────────────┘"""
content = content.replace(old_dep, new_dep)

# 4. 增加 第9节: WebSocket Server 与前端页面 (在第五章前追加)
websocket_section = """
---

### ⑨ WebSocket Server 与 Web 交互页面

#### [NEW] `myagent/interfaces/websocket/lock.py` — 会话级锁控制

```python
\"\"\"
SessionMutex：会话级并发锁控制模块。
防止同一 Session ID 接收并处理多个并发请求导致状态交错。
\"\"\"
import asyncio

class SessionMutex:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def acquire_session(self, session_id: str):
        return await self.get_lock(session_id).acquire()

    def release_session(self, session_id: str):
        if session_id in self._locks:
            self._locks[session_id].release()
```

#### [NEW] `myagent/interfaces/websocket/server.py` — WebSocket 服务器端

```python
\"\"\"
WebSocket Server：无需额外Web框架，纯使用 websockets 库。
集成 AgentCore 和配置信息，承接流式会话输出并下发给客户端，支持安全检查。
\"\"\"
import asyncio
import json
import websockets
from websockets.exceptions import ConnectionClosed

from myagent.core.agent import Agent
from myagent.interfaces.websocket.lock import SessionMutex
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class WebSocketServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port
        self.mutex = SessionMutex()

    async def _handle_connection(self, websocket):
        session_id = "default-ws-session"
        logger.info(f"Client connected, assigning session: {session_id}")

        async for message in websocket:
            data = json.loads(message)
            user_text = data.get("text", "")
            
            async with self.mutex.get_lock(session_id):
                # 调用 agent 获取 generator
                # 将流式数据封装成 JSON 通过 websocket推送
                # 此处省略具体业务代码，由 Agent 实现时填充 Agent 构建与循环控制逻辑。
                pass
                
    def start(self):
        start_server = websockets.serve(self._handle_connection, self.host, self.port)
        asyncio.get_event_loop().run_until_complete(start_server)
        logger.info(f"WebSocket Server running on ws://{self.host}:{self.port}")
        asyncio.get_event_loop().run_forever()

if __name__ == "__main__":
    server = WebSocketServer()
    server.start()
```

#### [NEW] 静态文件体系 (纯原生现代UI设计)

```text
web/
├── index.html       # 整体文档骨架与画布
├── css/
│   └── style.css    # 具有动态微交互，Glassmorphism 的高级主题设计，HSL高定色彩
└── js/
    └── app.js       # 原生 WebSocket 通信，控制 DOM 对话流刷新
```

**UI/UX 验收规则：**
必须呈现富UI体验，利用CSS变量构建Dark/Light现代调色板。应具备过渡动画效果 (如: tool 执行通过小 spinner 或者折叠面板表示，加载过程状态分级明确)。不要求任何三方库，完全用纯原生 HTML、JS 处理对话气泡、消息发送框等布局。

"""
content = "".join([content.split("## 五、数据流与执行路径")[0], websocket_section, "## 五、数据流与执行路径", content.split("## 五、数据流与执行路径")[1]])

# 5. 追加新文件
file_adds = """| 18 | `myagent/vision/image_handler.py` | 新建 | ImageHandler 图像处理器 |
| 19 | `myagent/interfaces/websocket/lock.py` | 新建 | SessionMutex 会话并发控制 |
| 20 | `myagent/interfaces/websocket/server.py` | 新建 | WebSocket 后端服务端 |
| 21 | `web/index.html` | 新建 | Web前端 UI 主页面 |
| 22 | `web/css/style.css` | 新建 | Web前端 UI 样式表 |
| 23 | `web/js/app.js` | 新建 | Web前端 UI 逻辑 |"""
content = content.replace("| 18 | `myagent/vision/image_handler.py` | 新建 | ImageHandler 图像处理器 |", file_adds)

# 6. 追加测试
test_adds = """| `vision/image_handler.py` | `test_image_handler.py` | 本地文件/URL/bytes 处理、格式检测、大小限制、不支持 vision 降级 |
| `interfaces/websocket/lock.py` | `test_ws_lock.py` | Mutex 并发防刷测试与状态一致性测试 |"""
content = content.replace("| `vision/image_handler.py` | `test_image_handler.py` | 本地文件/URL/bytes 处理、格式检测、大小限制、不支持 vision 降级 |", test_adds)

int_test_adds = """async def test_secret_redaction_in_audit():
    \"\"\"测试密钥在审计日志中被正确脱敏。\"\"\"

async def test_websocket_server_messaging():
    \"\"\"测试 WebSocket 全双工流式收发连接基础生命周期与消息解析。\"\"\""""
content = content.replace("""async def test_secret_redaction_in_audit():
    \"\"\"测试密钥在审计日志中被正确脱敏。\"\"\"""", int_test_adds)

# 7. 追加批次
batch_adds = """| **第 8 批** | `interfaces/cli/main.py`（修改）+ `config.yaml`（修改）| CLI 集成 + 配置更新（~200 行） |
| **第 9 批** | `interfaces/websocket/lock.py` + `interfaces/websocket/server.py` + `web/` 下的所有静态文件 | Web 界面互通建设（~300 行） |
| **第 10 批** | 全部测试文件 | 测试覆盖 |"""
content = content.replace("""| **第 8 批** | `interfaces/cli/main.py`（修改）+ `config.yaml`（修改）| CLI 集成 + 配置更新（~200 行） |
| **第 9 批** | 全部测试文件 | 测试覆盖 |""", batch_adds)

# 8. 追加特性
feat_adds = """| **ProviderEvent (Failover) 审计** | ✅ 完整实现 | `observability/hook.py` AuditHook 扩展 |
| **Web交互界面（原Phase4）** | ✅ 提前实现 | `interfaces/websocket/`目录以及`web/`端独立前端资源 |"""
content = content.replace("| **ProviderEvent (Failover) 审计** | ✅ 完整实现 | `observability/hook.py` AuditHook 扩展 |", feat_adds)

with open("phase2实施方案.md", "w", encoding="utf-8") as f:
    f.write(content)
print("修改完成")
