import pytest

from myagent.context.manager import ContextManager
from myagent.core.models import SessionData, UserContext
from myagent.core.session.manager import SessionManager
from myagent.prompt.renderer import PromptRenderer
from myagent.prompt.skills import SkillRegistry, load_skill_from_dir
from myagent.prompt.template import PromptTemplate
from myagent.prompt.variables import PromptVariables, VariableCollector
from myagent.tools.manager import ToolManager


def write_skill(root, username, name, body="正文说明", description="测试 Skill"):
    skill_dir = root / "prompts" / "skills" / username / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "allowed-tools: file_read, cli_execute\n"
        "---\n\n"
        f"# {name}\n\n"
        f"{body}\n"
        "目录变量: ${MYAGENT_SKILL_DIR}\n",
        encoding="utf-8",
    )
    return skill_dir


class FakeSession:
    def __init__(self, registry: SkillRegistry):
        self.data = SessionData()
        self.data.model.active = {
            "model_id": "test-model",
            "provider_type": "test",
            "context_window_size": 200000,
        }
        self.workspace = None
        self.context = ContextManager()
        self._skill_registry = registry


def make_manager(tmp_path) -> SessionManager:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agent:\n"
        "  skills:\n"
        "    enabled: true\n"
        "    active: []\n",
        encoding="utf-8",
    )
    return SessionManager(config_path=str(config_path))


def test_load_skill_from_lowercase_skill_md(tmp_path):
    skill_dir = write_skill(tmp_path, "default", "demo")

    skill = load_skill_from_dir(skill_dir, username="default")

    assert skill is not None
    assert skill.name == "demo"
    assert skill.description == "测试 Skill"
    assert skill.allowed_tools == ["file_read", "cli_execute"]
    assert str(skill.root_dir.resolve()) in skill.get_instructions()


def test_skill_registry_loads_only_requested_username(tmp_path):
    write_skill(tmp_path, "alice", "alice-only", body="alice 指令")
    write_skill(tmp_path, "bob", "bob-only", body="bob 指令")

    manager = make_manager(tmp_path)
    alice_registry = manager._build_skill_registry(UserContext(user_id="u1", username="alice"))
    bob_registry = manager._build_skill_registry(UserContext(user_id="u2", username="bob"))

    assert alice_registry.registered_names == ["alice-only"]
    assert bob_registry.registered_names == ["bob-only"]
    assert alice_registry.get("bob-only") is None


def test_skill_registry_uses_user_id_then_default(tmp_path):
    write_skill(tmp_path, "user-1", "by-user-id")
    write_skill(tmp_path, "default", "by-default")

    manager = make_manager(tmp_path)
    by_user_id = manager._build_skill_registry(UserContext(user_id="user-1", username=""))
    by_default = manager._build_skill_registry(UserContext(user_id="", username=""))

    assert by_user_id.registered_names == ["by-user-id"]
    assert by_default.registered_names == ["by-default"]


def test_skill_registry_active_filter_applies_within_username(tmp_path):
    write_skill(tmp_path, "alice", "enabled")
    write_skill(tmp_path, "alice", "disabled")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agent:\n"
        "  skills:\n"
        "    enabled: true\n"
        "    active:\n"
        "      - enabled\n",
        encoding="utf-8",
    )

    manager = SessionManager(config_path=str(config_path))
    registry = manager._build_skill_registry(UserContext(user_id="u1", username="alice"))

    assert registry.registered_names == ["enabled"]


@pytest.mark.asyncio
async def test_variable_collector_injects_file_backed_available_skills(tmp_path):
    write_skill(tmp_path, "default", "demo", description="文件 Skill")
    registry = SkillRegistry(username="default")
    registry.load_from_user_dir(tmp_path / "prompts" / "skills" / "default")
    session = FakeSession(registry)

    variables = await VariableCollector.collect(session)

    assert variables.available_skills == [
        {"name": "demo", "description": "文件 Skill", "username": "default"}
    ]
    assert variables.active_skills == []


def test_prompt_variables_render_skills_section():
    rendered = PromptRenderer(PromptTemplate.default()).render(
        PromptVariables(
            available_skills=[
                {
                    "name": "demo",
                    "description": "文件 Skill",
                    "username": "default",
                }
            ]
        )
    )

    assert "<skills>" in rendered
    assert "use_skill" in rendered
    assert "demo" in rendered


@pytest.mark.asyncio
async def test_use_skill_tool_executes_inline_with_file_backed_skill(tmp_path):
    skill_dir = write_skill(tmp_path, "default", "demo", body="详细指令")
    registry = SkillRegistry(username="default")
    registry.load_from_user_dir(tmp_path / "prompts" / "skills" / "default")
    manager = ToolManager(skill_registry=registry)

    assert "use_skill" in manager.tool_names
    result = await manager.execute("use_skill", skill_name="demo")

    assert not result.is_error
    assert result.metadata["skill_name"] == "demo"
    assert result.metadata["skill_dir"] == str(skill_dir)
    assert "详细指令" in result.content
    assert str(skill_dir.resolve()) in result.content


@pytest.mark.asyncio
async def test_use_skill_tool_reports_unknown_skill(tmp_path):
    write_skill(tmp_path, "default", "demo")
    registry = SkillRegistry(username="default")
    registry.load_from_user_dir(tmp_path / "prompts" / "skills" / "default")
    manager = ToolManager(skill_registry=registry)

    result = await manager.execute("use_skill", skill_name="missing")

    assert result.is_error
    assert "missing" in result.content
    assert "demo" in result.content


def test_use_skill_tool_is_hidden_without_registered_skills():
    manager = ToolManager(skill_registry=SkillRegistry())

    assert "use_skill" not in manager.tool_names
