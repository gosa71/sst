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
    "ollama": "qwen2.5-coder:7b",
    "lmstudio": "llama-3.1-8b-instruct",
    "local": "default",
}

class SSTSynthesizer:
    def __init__(self):
        self.provider = os.getenv("SST_PROVIDER", "openai").lower()
        self.base_url = os.getenv("SST_BASE_URL")
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

    def _build_prompt(self, func_key, scenarios):
        module_name = scenarios[0].get("module", func_key.rsplit(".", 1)[0])
        func_name = func_key.rsplit(".", 1)[-1]
        source = scenarios[0].get("source", "# source not available")
        deps = scenarios[0].get("dependencies", [])
        exec_meta = scenarios[0].get("execution_metadata", {})
        python_version = exec_meta.get("python_version", "unknown")

        scenario_text = ""
        for i, s in enumerate(scenarios):
            status = s["output"].get("status", "unknown")
            scenario_text += f"\n--- Scenario {i+1} (status: {status}) ---\n"
            scenario_text += f"Input: {json.dumps(s['input'], indent=2)}\n"
            if status == "success":
                scenario_text += f"Expected output: {json.dumps(s['output'].get('raw_result'), indent=2)}\n"
            else:
                scenario_text += f"Expected exception: {s['output'].get('error_type', 'Exception')}\n"
                scenario_text += f"Exception message: {s['output'].get('error', '')}\n"

        prompt = f"""You are a senior Python test engineer. Generate a complete, runnable Pytest test file.

## Function Under Test
```python
{source}
```

## Runtime context
- Python version: {python_version}
- Detected dependencies: {deps}
- Total captured scenarios: {len(scenarios)}
- Import the function as: from {module_name} import {func_name}

## Captured Scenarios (PII already masked — do not assert on masked values)
{scenario_text}

## Instructions
1. Generate a Pytest file that tests `{func_name}`.
2. Write one test function per scenario, named `test_{func_name}_scenario_N`.
   Captured input uses `args` (positional) and `kwargs` (keyword) — call the function as `{func_name}(*input["args"], **input["kwargs"])`.
3. For success scenarios: assert on the structure and deterministic values of the output dict.
4. For failure scenarios: use `pytest.raises(ExceptionType)` with the correct exception type.
5. Use `unittest.mock.patch` to mock any non-deterministic calls (random, datetime, uuid).
6. Use `freezegun.freeze_time` if output contains timestamps.
7. Skip assertions on values that are masked strings like `[MASKED_EMAIL]` — only check they are strings.
8. Include all required imports at the top.
9. Do NOT use self-correction or retry logic — generate correct code on the first attempt.
10. Output ONLY valid Python code, no explanations, no markdown fences.

The output must be syntactically valid Python that passes `python -m py_compile`."""

        return prompt

    def _validate_syntax(self, code: str, func_key: str) -> bool:
        """Return True if code is valid Python syntax, False otherwise."""
        try:
            compile(code, f"<generated:{func_key}>", "exec")
            return True
        except SyntaxError as e:
            logger.warning(
                "SST: Generated test for '%s' has syntax error at line %d: %s",
                func_key, e.lineno, e.msg
            )
            return False

    def _call_llm(self, prompt):
        if self.provider == "openai":
            return self._call_openai(prompt)
        if self.provider == "anthropic":
            return self._call_anthropic(prompt)
        if self.provider in {"ollama", "lmstudio", "local"}:
            return self._call_local_llm(prompt)
        raise ValueError(f"Unknown provider: {self.provider}")


    def _call_local_llm(self, prompt):
        from openai import OpenAI

        if self.base_url:
            base_url = self.base_url
        elif self.provider == "ollama":
            base_url = "http://localhost:11434/v1"
        elif self.provider == "lmstudio":
            base_url = "http://localhost:1234/v1"
        else:
            raise ValueError(f"Provider '{self.provider}' requires SST_BASE_URL environment variable")

        client = OpenAI(base_url=base_url, api_key="not-needed")
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a Python test generation expert. Output only valid Python code."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=4096,
        )
        content = response.choices[0].message.content
        if content.startswith("```python"):
            content = content[len("```python"):].strip()
        if content.endswith("```"):
            content = content[:-3].strip()
        return content

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
            logger.info("No captures found.")
            return

        groups = self._group_by_function(captures)
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        init_path = os.path.join(output_dir, "__init__.py")
        if not os.path.exists(init_path):
            Path(init_path).touch(exist_ok=True)

        for func_key, scenarios in groups.items():
            logger.info("Generating tests for %s (%d scenarios)...", func_key, len(scenarios))
            prompt = self._build_prompt(func_key, scenarios)
            
            try:
                test_code = self._call_llm(prompt)
            except Exception as e:
                logger.warning("LLM call failed for %s: %s", func_key, e)
                logger.info("Generating fallback template for %s", func_key)
                test_code = self._generate_fallback(func_key, scenarios)

            if not self._validate_syntax(test_code, func_key):
                logger.warning("Generated code for %s has syntax errors; falling back to template", func_key)
                test_code = self._generate_fallback(func_key, scenarios)

            safe_name = func_key.replace(".", "_")
            output_path = os.path.join(output_dir, f"test_{safe_name}.py")
            
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(test_code)
            logger.info("Written to %s", output_path)

            if open_editor:
                editor = os.getenv("EDITOR", "nano")
                subprocess.run([editor, output_path])

    def _generate_fallback(self, func_key, scenarios):
        module_name = func_key.rsplit(".", 1)[0]
        func_name = func_key.rsplit(".", 1)[1]
        
        code = f'''import pytest
from unittest.mock import patch
try:
    from freezegun import freeze_time
except ImportError:
    freeze_time = None  # freezegun not installed; skip time-freezing tests
from {module_name} import {func_name}

'''
        for i, s in enumerate(scenarios):
            code += f'''\
def test_{func_name}_scenario_{i+1}():
    """Auto-generated from captured shadow data."""
    # Input: {json.dumps(s["input"])}
    # Expected output status: {s["output"].get("status", "unknown")}
    # TODO: Fill in the test logic based on the captured data above.
    pass
'''
        return code
