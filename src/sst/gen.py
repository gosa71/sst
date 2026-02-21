"""
Lightweight test generator that creates test stubs without LLM.
Useful for offline/CI environments where LLM access is unavailable.
"""
import json
import os
import glob
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SSTGen:
    def __init__(self, shadow_dir=".shadow_data", output_dir="tests/"):
        self.shadow_dir = shadow_dir
        self.output_dir = output_dir

    def _load_captures(self, func_filter=None):
        files = sorted(glob.glob(os.path.join(self.shadow_dir, "*.json")))
        captures = []
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
            except json.JSONDecodeError as e:
                logger.warning("Skipping corrupted JSON file %s: %s", f, e)
                continue
            required = {"function", "module", "semantic_id", "input", "output"}
            missing = required - data.keys()
            if missing:
                logger.warning("Skipping %s: missing fields %s", f, sorted(missing))
                continue
            if func_filter and data.get("function") != func_filter:
                continue
            captures.append(data)
        return captures

    def _group_by_function(self, captures):
        groups = {}
        for c in captures:
            module = c.get("module", "")
            function = c.get("function", "")
            if not module or not function:
                logger.debug("Skipping malformed capture in grouping: %s", c)
                continue
            key = f"{module}.{function}"
            if key not in groups:
                groups[key] = []
            groups[key].append(c)
        return groups

    def _generate_test_code(self, func_key, scenarios):
        module_name = func_key.rsplit(".", 1)[0]
        func_name = func_key.rsplit(".", 1)[1]

        lines = [
            "import pytest",
            "from unittest.mock import patch, MagicMock",
            "from freezegun import freeze_time",
            f"from {module_name} import {func_name}",
            "",
            "",
        ]

        for i, s in enumerate(scenarios):
            input_data = s.get("input", {})
            output_data = s.get("output", {})
            status = output_data.get("status", "unknown")
            semantic_id = s.get("semantic_id", "unknown")

            lines.append(f"class TestSST_{func_name}_scenario_{i+1}:")
            lines.append(f'    """')
            lines.append(f"    Auto-generated from shadow capture.")
            lines.append(f"    Semantic ID: {semantic_id}")
            lines.append(f"    Status: {status}")
            lines.append(f'    """')
            lines.append("")

            if status == "success":
                raw_result = output_data.get("raw_result", {})
                lines.append(f"    def test_returns_expected_structure(self):")
                lines.append(f"        # TODO: reconstruct input args from captured data")
                lines.append(f"        # Input: {json.dumps(input_data)}")
                lines.append(f"        # Expected keys in result: {list(raw_result.keys()) if isinstance(raw_result, dict) else 'N/A'}")
                lines.append(f"        pass")
                lines.append("")

                if isinstance(raw_result, dict):
                    lines.append(f"    def test_output_keys(self):")
                    lines.append(f"        # Assert the result contains the expected keys")
                    lines.append(f"        expected_keys = {list(raw_result.keys())}")
                    lines.append(f"        # result = {func_name}(...)")
                    lines.append(f"        # assert set(result.keys()) == set(expected_keys)")
                    lines.append(f"        pass")
                    lines.append("")

            elif status == "failure":
                error_type = output_data.get("error_type", "Exception")
                lines.append(f"    def test_raises_{error_type.lower()}(self):")
                lines.append(f"        # This scenario raised {error_type}: {output_data.get('error', '')}")
                lines.append(f"        # with pytest.raises({error_type}):")
                lines.append(f"        #     {func_name}(...)")
                lines.append(f"        pass")
                lines.append("")

            lines.append("")

        return "\n".join(lines)

    def run(self, func_filter=None):
        captures = self._load_captures(func_filter)
        if not captures:
            logger.info("No captures found.")
            return

        groups = self._group_by_function(captures)

        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        
        init_path = os.path.join(self.output_dir, "__init__.py")
        if not os.path.exists(init_path):
            Path(init_path).touch(exist_ok=True)

        for func_key, scenarios in groups.items():
            logger.info("Generating stub tests for %s (%d scenarios)...", func_key, len(scenarios))
            test_code = self._generate_test_code(func_key, scenarios)

            safe_name = func_key.replace(".", "_")
            output_path = os.path.join(self.output_dir, f"test_{safe_name}_stub.py")

            with open(output_path, "w", encoding="utf-8") as f:
                f.write(test_code)
            logger.info("  Written to %s", output_path)


if __name__ == "__main__":
    import sys
    func = sys.argv[1] if len(sys.argv) > 1 else None
    SSTGen().run(func_filter=func)
