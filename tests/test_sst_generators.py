"""Tests for SSTGen (offline stub generator) and SSTSynthesizer (LLM-backed generator)."""
import inspect
import json

import pytest
from unittest.mock import patch

from sst.gen import SSTGen
from sst.synthesizer import SSTSynthesizer, _PROVIDER_DEFAULT_MODELS


@pytest.fixture
def capture_dir(tmp_path):
    """A temp shadow_dir with two realistic capture files."""
    captures = [
        {
            "function": "calculate",
            "module": "myapp.billing",
            "semantic_id": "abc123",
            "input": {"args": [10, 2], "kwargs": {}},
            "output": {"raw_result": {"total": 20}, "status": "success"},
            "source": "def calculate(a, b): return {'total': a * b}",
            "dependencies": [],
        },
        {
            "function": "calculate",
            "module": "myapp.billing",
            "semantic_id": "def456",
            "input": {"args": [0, 5], "kwargs": {}},
            "output": {
                "error": "division by zero",
                "error_type": "ZeroDivisionError",
                "status": "failure",
            },
            "source": "def calculate(a, b): return {'total': a * b}",
            "dependencies": [],
        },
    ]
    for cap in captures:
        path = tmp_path / f"{cap['module']}.{cap['function']}_{cap['semantic_id']}.json"
        path.write_text(json.dumps(cap), encoding="utf-8")
    return tmp_path


class TestSSTGen:
    def test_load_captures_returns_all_files(self, capture_dir):
        gen = SSTGen(shadow_dir=str(capture_dir))
        captures = gen._load_captures()
        assert len(captures) == 2

    def test_load_captures_filters_by_function(self, capture_dir):
        gen = SSTGen(shadow_dir=str(capture_dir))
        captures = gen._load_captures(func_filter="calculate")
        assert all(c["function"] == "calculate" for c in captures)

    def test_load_captures_skips_corrupted_json(self, tmp_path):
        (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
        gen = SSTGen(shadow_dir=str(tmp_path))
        captures = gen._load_captures()
        assert captures == []

    def test_group_by_function(self, capture_dir):
        gen = SSTGen(shadow_dir=str(capture_dir))
        captures = gen._load_captures()
        groups = gen._group_by_function(captures)
        assert "myapp.billing.calculate" in groups
        assert len(groups["myapp.billing.calculate"]) == 2

    def test_generate_test_code_success_scenario(self, capture_dir):
        gen = SSTGen(shadow_dir=str(capture_dir))
        captures = gen._load_captures()
        groups = gen._group_by_function(captures)
        code = gen._generate_test_code("myapp.billing.calculate", groups["myapp.billing.calculate"])
        assert "import pytest" in code
        assert "class TestSST_calculate_scenario_" in code
        assert "test_returns_expected_structure" in code

    def test_generate_test_code_failure_scenario(self, capture_dir):
        gen = SSTGen(shadow_dir=str(capture_dir))
        captures = gen._load_captures()
        groups = gen._group_by_function(captures)
        code = gen._generate_test_code("myapp.billing.calculate", groups["myapp.billing.calculate"])
        assert "test_raises_zerodivisionerror" in code

    def test_run_writes_output_files(self, capture_dir, tmp_path):
        output_dir = tmp_path / "out"
        gen = SSTGen(shadow_dir=str(capture_dir), output_dir=str(output_dir))
        gen.run()
        written = list(output_dir.glob("test_*.py"))
        assert len(written) == 1
        assert "myapp_billing_calculate" in written[0].name

    def test_run_creates_init_file(self, capture_dir, tmp_path):
        output_dir = tmp_path / "out"
        gen = SSTGen(shadow_dir=str(capture_dir), output_dir=str(output_dir))
        gen.run()
        assert (output_dir / "__init__.py").exists()

    def test_run_with_no_captures_logs_message(self, tmp_path, caplog):
        gen = SSTGen(shadow_dir=str(tmp_path))
        with caplog.at_level("INFO"):
            gen.run()
        assert "No captures found" in caplog.text


class TestSSTSynthesizer:
    def test_default_model_openai(self, monkeypatch):
        monkeypatch.delenv("SST_PROVIDER", raising=False)
        monkeypatch.delenv("SST_MODEL", raising=False)
        s = SSTSynthesizer()
        assert s.provider == "openai"
        assert s.model == _PROVIDER_DEFAULT_MODELS["openai"]

    def test_default_model_anthropic(self, monkeypatch):
        monkeypatch.setenv("SST_PROVIDER", "anthropic")
        monkeypatch.delenv("SST_MODEL", raising=False)
        s = SSTSynthesizer()
        assert s.model == _PROVIDER_DEFAULT_MODELS["anthropic"]

    def test_model_env_override(self, monkeypatch):
        monkeypatch.setenv("SST_PROVIDER", "anthropic")
        monkeypatch.setenv("SST_MODEL", "claude-haiku-4-20251001")
        s = SSTSynthesizer()
        assert s.model == "claude-haiku-4-20251001"

    def test_no_stale_claude3_sonnet_reference(self):
        """Regression guard: old model string must not appear anywhere in the module."""
        import sst.synthesizer as mod

        source = inspect.getsource(mod)
        assert "claude-3-sonnet-20240229" not in source

    def test_build_prompt_contains_source_and_scenarios(self):
        s = SSTSynthesizer()
        captures = [
            {
                "function": "calculate",
                "module": "myapp.billing",
                "semantic_id": "abc123",
                "input": {"args": [10, 2], "kwargs": {}},
                "output": {"raw_result": {"total": 20}, "status": "success"},
                "source": "def calculate(a, b): return {'total': a * b}",
                "dependencies": ["math"],
            }
        ]
        prompt = s._build_prompt("myapp.billing.calculate", captures)
        assert "def calculate" in prompt
        assert "Scenario 1" in prompt
        assert "math" in prompt

    def test_run_uses_llm_and_writes_file(self, tmp_path):
        output_dir = tmp_path / "tests"
        s = SSTSynthesizer()

        with (
            patch.object(s, "_load_captures") as mock_load,
            patch.object(s, "_call_llm", return_value="# generated test code") as mock_llm,
        ):
            mock_load.return_value = [
                {
                    "function": "calculate",
                    "module": "myapp.billing",
                    "semantic_id": "abc123",
                    "input": {},
                    "output": {"status": "success"},
                    "source": "def calculate(): pass",
                    "dependencies": [],
                }
            ]
            s.run(output_dir=str(output_dir))

        mock_llm.assert_called_once()
        written = list(output_dir.glob("test_*.py"))
        assert len(written) == 1
        assert written[0].read_text() == "# generated test code"

    def test_run_falls_back_when_llm_fails(self, tmp_path):
        output_dir = tmp_path / "tests"
        s = SSTSynthesizer()

        with (
            patch.object(s, "_load_captures") as mock_load,
            patch.object(s, "_call_llm", side_effect=RuntimeError("API down")),
        ):
            mock_load.return_value = [
                {
                    "function": "calculate",
                    "module": "myapp.billing",
                    "semantic_id": "abc123",
                    "input": {},
                    "output": {"status": "success"},
                    "source": "def calculate(): pass",
                    "dependencies": [],
                }
            ]
            s.run(output_dir=str(output_dir))

        written = list(output_dir.glob("test_*.py"))
        assert len(written) == 1
        content = written[0].read_text()
        assert "import pytest" in content

    def test_call_llm_raises_on_unknown_provider(self, monkeypatch):
        monkeypatch.setenv("SST_PROVIDER", "unknown_provider")
        s = SSTSynthesizer()
        with pytest.raises(ValueError, match="Unknown provider"):
            s._call_llm("some prompt")

    def test_load_captures_uses_config_shadow_dir(self, tmp_path, monkeypatch):
        """SSTSynthesizer must respect shadow_dir from config, not a hardcoded path."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.sst]\nshadow_dir = "custom_shadow"\n', encoding="utf-8"
        )
        from sst.config import refresh_config

        refresh_config()

        custom_shadow = tmp_path / "custom_shadow"
        custom_shadow.mkdir()
        cap = {
            "function": "fn",
            "module": "mod",
            "semantic_id": "x1",
            "input": {},
            "output": {"status": "success"},
        }
        (custom_shadow / "mod.fn_x1.json").write_text(json.dumps(cap), encoding="utf-8")

        s = SSTSynthesizer()
        captures = s._load_captures()
        assert len(captures) == 1
        assert captures[0]["function"] == "fn"
