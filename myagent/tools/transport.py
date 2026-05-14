"""
Transport 后端抽象：子进程 / TCP（Docker/远程）连接。
"""
import asyncio
import atexit
import logging
import os
import signal
import sys
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Transport(ABC):

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @property
    @abstractmethod
    def reader(self) -> asyncio.StreamReader: ...

    @property
    @abstractmethod
    def writer(self) -> asyncio.StreamWriter: ...

    @staticmethod
    def build_safe_env() -> dict[str, str]:
        env = dict(os.environ)
        for key in (
            "API_KEY", "SECRET_KEY", "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
        ):
            env.pop(key, None)
        return env


class SubprocessTransport(Transport):
    """
    本地 Python 子进程传输层。

    主进程 spawn 子进程，通过 stdin/stdout 管道通信。
    注册信号和 atexit，确保主进程退出时子进程被 kill。
    """

    # 100MB — 允许大型工具结果（如 PDF 渲染为 base64）通过 JSON-RPC 管道
    _READER_LIMIT = 100 * 1024 * 1024

    def __init__(self, *, cwd: str | None = None, env: dict | None = None,
                 max_output_bytes: int = 102400):
        self._cwd = cwd or os.getcwd()
        self._env = env or Transport.build_safe_env()
        self._env["MYAGENT_SANDBOX_BACKEND"] = "subprocess"
        self._env["MYAGENT_MAX_OUTPUT_BYTES"] = str(max_output_bytes)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._drain_task: asyncio.Task | None = None
        self._exit_hooks_registered = False

    @property
    def reader(self) -> asyncio.StreamReader:
        assert self._reader is not None, "Transport not started"
        return self._reader

    @property
    def writer(self) -> asyncio.StreamWriter:
        assert self._writer is not None, "Transport not started"
        return self._writer

    async def start(self) -> None:
        # ── 幂等保护：防止 start() 被重复调用 ──
        #
        # 背景：当前调用链中，Transport.start() 会被调用两次：
        #   1. try_create_transport() 中先调用 start() — 目的是验证 transport 可用
        #      （Docker TCP 场景需要探测连接，subprocess 场景并不需要但也会执行）
        #   2. JsonRpcProxy.start() 中再次调用 self._transport.start() — 作为统一生命周期入口
        #
        # 不加保护时，第二次 start() 会：
        #   - 创建新的子进程，覆盖 self._proc（旧子进程成为孤儿）
        #   - 创建新的 _drain_task，覆盖旧 task（旧 task 永远不会被 await/cancel）
        #   - 导致 "Task was destroyed but it is pending!" 警告
        #
        # TODO: 未来优化方向 — 让 try_create_transport() 只负责创建、不负责启动，
        #       将 start() 统一交给 JsonRpcProxy 管理，届时可移除此保护。
        if self._proc is not None:
            logger.debug("SubprocessTransport.start() skipped: already started (idempotent guard)")
            return

        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "myagent.tools.runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            limit=self._READER_LIMIT,
        )
        logger.info(f"SubprocessTransport started: PID={self._proc.pid}")

        self._register_exit_hooks()

        self._reader = self._proc.stdout
        self._writer = self._proc.stdin

        self._drain_task = asyncio.create_task(self._drain_stderr())

    def _register_exit_hooks(self) -> None:
        if self._exit_hooks_registered:
            return
        self._exit_hooks_registered = True

        def _cleanup():
            if self._proc and self._proc.returncode is None:
                try:
                    self._proc.kill()
                    logger.info(
                        f"SubprocessTransport cleanup: killed PID={self._proc.pid}")
                except Exception:
                    pass

        atexit.register(_cleanup)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                def _signal_handler(s=sig):
                    _cleanup()
                    # 移除自定义 handler，恢复默认行为，让进程能正常退出
                    loop.remove_signal_handler(s)
                    # 向自身重新发送信号，触发默认处理（KeyboardInterrupt / 终止）
                    os.kill(os.getpid(), s)
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass

    async def stop(self) -> None:
        if self._drain_task:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass

        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass

        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass

        logger.info("SubprocessTransport stopped")

    async def _drain_stderr(self) -> None:
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.info(f"[child] {line.decode().rstrip()}")
        except Exception:
            pass


class TcpTransport(Transport):
    """
    TCP 连接传输层。

    连接到已运行的 JSON-RPC 服务器（Docker 容器或远程机器）。
    不管理服务器生命周期。stop() 只关闭客户端连接。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9876):
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def reader(self) -> asyncio.StreamReader:
        assert self._reader is not None, "Transport not started"
        return self._reader

    @property
    def writer(self) -> asyncio.StreamWriter:
        assert self._writer is not None, "Transport not started"
        return self._writer

    async def start(self) -> None:
        # ── 幂等保护：防止 start() 被重复调用 ──
        # 原因同 SubprocessTransport.start() 中的注释：
        # try_create_transport() 和 JsonRpcProxy.start() 都会调用 start()。
        # TCP 场景下，try_create_transport() 中已经真正建立了连接（用于探测 Docker 可用性），
        # JsonRpcProxy.start() 再次调用时不应重复建连。
        if self._reader is not None:
            logger.debug("TcpTransport.start() skipped: already connected (idempotent guard)")
            return

        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port,
            limit=SubprocessTransport._READER_LIMIT,
        )
        logger.info(f"TcpTransport connected: {self._host}:{self._port}")

    async def stop(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        logger.info(f"TcpTransport disconnected: {self._host}:{self._port}")


async def try_create_transport(runner_config: dict) -> tuple[Transport, str]:
    """
    按优先级尝试创建 Transport：

    1. docker:  尝试 TCP 连接 Docker 容器 -> 成功则返回 TcpTransport
    2. docker 连接失败 -> 记录日志 -> 降级到 SubprocessTransport

    Returns:
        (transport, backend_name)  -- backend_name in {"docker", "subprocess"}
    """
    backend = runner_config.get("backend", "auto")

    if backend in ("auto", "docker"):
        docker_cfg = runner_config.get("docker", {})
        host = docker_cfg.get("host", "127.0.0.1")
        port = docker_cfg.get("port", 9876)
        connect_timeout = docker_cfg.get("connect_timeout", 3.0)

        try:
            tcp_transport = TcpTransport(host=host, port=port)
            await asyncio.wait_for(tcp_transport.start(), timeout=connect_timeout)
            logger.info(f"Docker executor available at {host}:{port}")
            return tcp_transport, "docker"
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
            logger.warning(
                f"Docker executor not available at {host}:{port} ({e}). "
                f"Falling back to subprocess."
            )

    subprocess_cfg = runner_config.get("subprocess", {})
    env = Transport.build_safe_env()
    subprocess_transport = SubprocessTransport(
        cwd=os.getcwd(),
        env=env,
        max_output_bytes=subprocess_cfg.get("max_output_bytes", 102400),
    )
    await subprocess_transport.start()
    logger.info("Using subprocess backend (fallback)")
    return subprocess_transport, "subprocess"
