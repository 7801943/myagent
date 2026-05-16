"""Quick import test for the Pydantic SessionData refactoring."""
print("Testing imports...")

from myagent.core.models import SessionState, AgentRunState, UserContext, SessionData
from myagent.core.models import TokenUsage, UserInfo, ModelInfo, ToolInfo, SessionContext, WorkspaceInfo
print("  models.py imports OK")

from myagent.context.state import StateStore, SQLiteStateStore, AgentState
print("  state.py imports OK")

from myagent.core.session import Session, SessionManager
print("  session.py imports OK")

from myagent.core import SessionData, UserContext
print("  __init__.py imports OK")

# Test backward compat alias
assert AgentState is AgentRunState
print("  AgentState alias OK")

# Test Pydantic SessionData creation
sd = SessionData()
print("  SessionData() creation OK")

# Test attribute access (new style)
assert sd.context.agent_run_state == "idle"
assert sd.context.session_state == "active"
assert sd.user.user_id == ""
assert sd.context.token_usage.used == 0
assert sd.context.token_usage.total == 128000
print("  Attribute access OK")

# Test computed fields
assert sd.context.token_usage.percentage == 0.0
assert sd.context.token_usage.remaining == 128000
print("  Computed fields OK")

# Test model_dump
d = sd.model_dump()
assert "user" in d
assert "context" in d
assert d["context"]["agent_run_state"] == "idle"
assert "percentage" in d["context"]["token_usage"]
print("  model_dump() OK")

d2 = sd.model_dump()
assert d2["context"]["token_usage"]["used"] == 0
print("  model_dump() OK")

# Test model_validate (new format)
sd3 = SessionData.model_validate(d)
assert sd3.context.agent_run_state == "idle"
print("  model_validate() OK")

# Test extra dict access
sd.extra["foo"] = "bar"
assert sd.extra.get("foo") == "bar"
print("  extra dict OK")

print("\nAll tests passed.")