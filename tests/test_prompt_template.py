from pathlib import Path

from myagent.core.session.manager import SessionManager
from myagent.prompt.variables import PromptVariables


def test_session_manager_loads_prompt_template_from_config_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / "app"
    template_dir = config_dir / "config"
    template_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    template_path = template_dir / "prompt_template.yaml"

    config_path.write_text(
        "agent:\n"
        "  prompt_template_path: config/prompt_template.yaml\n"
        "  providers:\n"
        "    - name: test\n"
        "      type: openai\n"
        "      model: test-model\n",
        encoding="utf-8",
    )
    template_path.write_text(
        "version: '2.0'\n"
        "sections:\n"
        "  - name: identity\n"
        "    priority: high\n"
        "    enabled_when: true\n"
        "    template: |\n"
        "      <identity>\n"
        "      custom template loaded\n"
        "      </identity>\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(Path("/tmp"))

    manager = SessionManager(config_path=str(config_path))
    rendered = manager.create_prompt_renderer().render(PromptVariables())

    assert "custom template loaded" in rendered
