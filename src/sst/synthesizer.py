import json
import os
import glob
import logging
import subprocess
from pathlib import Path

from .config import get_config

logger = logging.getLogger(__name__)

_PROVIDER_DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",
}

class SSTSynthesizer:
    def __init__(self):
        self.provider = os.getenv("SST_PROVIDER", "openai")
        default_model = _PROVIDER_DEFAULT_MODELS.get(self.provider, "gpt-4o")
        self.model = os.getenv("SST_MODEL", default_model)

    def _load_captures(self, func_filter=None):
        shadow_dir = get_config().shadow_dir
        files = glob.glob(os.path.join(shadow_dir, "*.json"))
        captures = []
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
            except json.JSONDecodeError as e:
                logger.warning("Skipping corrupted JSON file %s: %s", f, e)
                continue
            if func_filter and data["function"] != func_filter:
                continue
            captures.append(data)
        return captures

    def _group_by_function(self, captures):
        groups = {}
        for c in captures:
            key = f"{c['module']}.{c['function']}"
            if key not in groups:
                groups[key] = []
            groups[key].append(c)
        return groups

    def _build_prompt(self, func_key, scenarios):
        source = scenarios[0].get("source", "# source not available")
        deps = scenarios[0].get("dependencies", [])
        
        scenario_text = ""
        for i, s in enumerate(scenarios):
            scenario_text += f"\n--- Scenario {i+1} ---\n"
            scenario_text += f"Input: {json.dumps(s['input'], indent=2)}\n"
            scenario_text += f"Output: {json.dumps(s['output'], indent=2)}\n"

        prompt = f"""You are a senior Python test engineer. Generate a complete, runnable Pytest test file.

## Function Under Test
```python
{source}
```

## Dependencies detected: {deps}

## Captured Scenarios (PII already masked)
{scenario_text}

## Instructions
1. Generate a Pytest file that tests the function `{func_key.split('.')[-1]}`.
2. Use `freezegun` to freeze time if the output contains timestamps.
3. Use `unittest.mock.patch` to mock `random.randint` or any non-deterministic calls.
4. For each scenario, write a separate test function.
5. Assert on the STRUCTURE and DETERMINISTIC values of the output.
6. Skip assertions on masked PII values - just check they exist and are strings.
7. Include proper imports at the top.
8. Make the tests self-contained and runnable with `pytest`.

Generate ONLY the Python code, no explanations."""

        return prompt

    def _call_llm(self, prompt):
        if self.provider == "openai":
            return self._call_openai(prompt)
        elif self.provider == "anthropic":
            return self._call_anthropic(prompt)
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

    def _call_openai(self, prompt):
        from openai import OpenAI
        client = OpenAI()
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a Python test generation expert. Output only valid Python code."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2
        )
        content = response.choices[0].message.content
        if content.startswith("```python"):
            content = content[len("```python"):].strip()
        if content.endswith("```"):
            content = content[:-3].strip()
        return content

    def _call_anthropic(self, prompt):
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        content = response.content[0].text
        if content.startswith("```python"):
            content = content[len("```python"):].strip()
        if content.endswith("```"):
            content = content[:-3].strip()
        return content

    def run(self, func_filter=None, output_dir="tests/", open_editor=False):
        captures = self._load_captures(func_filter)
        if not captures:
            print("No captures found.")
            return

        groups = self._group_by_function(captures)
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        init_path = os.path.join(output_dir, "__init__.py")
        if not os.path.exists(init_path):
            Path(init_path).touch(exist_ok=True)

        for func_key, scenarios in groups.items():
            print(f"Generating tests for {func_key} ({len(scenarios)} scenarios)...")
            prompt = self._build_prompt(func_key, scenarios)
            
            try:
                test_code = self._call_llm(prompt)
            except Exception as e:
                print(f"  LLM call failed: {e}")
                print("  Generating fallback template...")
                test_code = self._generate_fallback(func_key, scenarios)

            safe_name = func_key.replace(".", "_")
            output_path = os.path.join(output_dir, f"test_{safe_name}.py")
            
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(test_code)
            print(f"  Written to {output_path}")

            if open_editor:
                editor = os.getenv("EDITOR", "nano")
                subprocess.run([editor, output_path])

    def _generate_fallback(self, func_key, scenarios):
        module_name = func_key.rsplit(".", 1)[0]
        func_name = func_key.rsplit(".", 1)[1]
        
        code = f'''import pytest
from unittest.mock import patch
from freezegun import freeze_time
from {module_name} import {func_name}

'''
        for i, s in enumerate(scenarios):
            code += f'''\
def test_{func_name}_scenario_{i+1}():
    """Auto-generated from captured shadow data."""
    # Input: {json.dumps(s["input"], indent=4)}
    # Expected output status: {s["output"].get("status", "unknown")}
    # TODO: Fill in the test logic based on the captured data above.
    pass
'''
        return code
