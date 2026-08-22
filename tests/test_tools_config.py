from myagent.utils.config import AgentConfig


def test_tools_config_parses_global_hidden_tools():
    config = AgentConfig(
        tools={
            "default_timeout": 20,
            "hidden_tools": ["file_read", "file_edit"],
        }
    )

    assert config.tools.default_timeout == 20
    assert config.tools.hidden_tools == ["file_read", "file_edit"]
