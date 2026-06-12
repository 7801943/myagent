import asyncio
from myagent.tools.engine import ExecutionEngine

async def main():
    engine = ExecutionEngine(sandbox_backend="subprocess")
    module = engine._load_module("test", "/home/zhouxiang/myagent/myagent/tools/builtin/file_query.py")
    fn = module.file_query
    await fn(path="/home/zhouxiang/myagent/scratch_test.py", query="test")

if __name__ == "__main__":
    asyncio.run(main())
