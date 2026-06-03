"""Tool 执行器子进程/服务器入口。

两种模式：
  python -m myagent.tools.runner              -> pipe 模式（SubprocessTransport spawn 用）
  python -m myagent.tools.runner --tcp 9876   -> TCP 模式（Docker/独立部署用）

─────────────────────────────────────────
Docker 独立部署：
─────────────────────────────────────────
1. 构建镜像：
     docker build -t myagent-sandbox:latest -f Dockerfile.sandbox .

2. 启动容器（TCP 模式，容器作为服务器常驻）：
     docker run -d --name myagent-executor \
       -p 9876:9876 \
       -v $(pwd):/workspace:rw \
       -w /workspace \
       --memory=512m --cpus=1 --pids-limit=50 \
       --security-opt=no-new-privileges \
       myagent-sandbox:latest \
       python -m myagent.tools.runner --tcp --port 9876

3. 容器由用户或 daemon 管理生命周期：
     docker logs myagent-executor
     docker stop myagent-executor
     docker rm myagent-executor

4. 主进程连接：config.yaml sandbox.backend=docker，指向 127.0.0.1:9876
─────────────────────────────────────────
"""
import argparse
import asyncio
import logging
import os
import sys

from myagent.tools.engine import ExecutionEngine
from myagent.tools.json_rpc import JsonRpcServer

# 子进程日志配置：输出到 stderr，主进程的 _drain_stderr 会捕获并转发
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    stream=sys.stderr,
)

# 屏蔽第三方库的 DEBUG 噪音（PIL、httpx 等）
for _noisy in ("PIL", "httpx", "httpcore", "urllib3", "matplotlib"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


async def _pipe_main() -> None:
    backend = os.environ.get("MYAGENT_SANDBOX_BACKEND", "subprocess")
    max_output_bytes = int(os.environ.get(
        "MYAGENT_MAX_OUTPUT_BYTES", "102400"))
    engine = ExecutionEngine(
        sandbox_backend=backend, max_output_bytes=max_output_bytes)
    server = JsonRpcServer(engine)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)

    await server.serve(reader, writer)


async def _tcp_main(host: str = "0.0.0.0", port: int = 9876) -> None:
    backend = os.environ.get("MYAGENT_SANDBOX_BACKEND", "docker")
    engine = ExecutionEngine(sandbox_backend=backend)
    server = JsonRpcServer(engine)

    async def handle_client(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        await server.serve(reader, writer)

    srv = await asyncio.start_server(handle_client, host, port)
    addr = srv.sockets[0].getsockname()
    print(f"JsonRpcServer listening on tcp://{addr[0]}:{addr[1]}",
          file=sys.stderr)

    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MyAgent Tool Execution Server")
    parser.add_argument("--tcp", action="store_true",
                        help="Run as TCP server (default: pipe mode)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="TCP bind address")
    parser.add_argument("--port", type=int, default=9876,
                        help="TCP port")
    args = parser.parse_args()

    if args.tcp:
        asyncio.run(_tcp_main(args.host, args.port))
    else:
        asyncio.run(_pipe_main())
