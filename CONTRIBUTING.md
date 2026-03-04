# Contributing to SST

Bug fixes, new features, and documentation improvements are all welcome.

## Setup

```bash
git clone https://github.com/gosa71/sst
cd sst
pip install -e ".[fastapi,llm,test]"
```

The project uses `src/` layout — all source lives in `src/sst/`.

## Running tests

```bash
pytest                          # full suite
pytest tests/test_sst_meta.py  # single file
pytest -m "not e2e"            # skip slow end-to-end tests
```

Tests require no external services. The `[test]` extra installs
`pytest` and `freezegun`. The `[fastapi]` extra installs `starlette`
which is required for `tests/test_sst_middleware.py` — that file is
skipped automatically when starlette is not installed.

## Project structure

```
src/sst/
  core.py          # @sst.capture decorator, SSTCore, serialization, PII masking
  middleware.py    # SSTMiddleware — HTTP capture for FastAPI / Starlette
  diff.py          # DiffPolicy, structured diff engine
  replay.py        # ReplayEngine — baseline vs capture comparison
  governance.py    # lifecycle transitions, approval history
  gen.py           # AI test generation (optional LLM dependency)
  cli.py           # sst CLI entry point
  config.py        # pyproject.toml loader
  types.py         # CapturePayload, BaselineRecord, and other dataclasses
  schema.py        # JSON schema validation

tests/
  test_sst_meta.py
  test_sst_middleware.py
  test_sst_diff.py
  test_sst_governance.py
  test_sst_replay.py
  ... (one file per module)
```

## Areas for contribution

**PII masking** — add regex patterns or key names in `src/sst/core.py`
(`_CaptureNormalizer`). Pattern format: `{label, pattern}` dict, same
as `[tool.sst.pii_patterns]` in `pyproject.toml`.

**LLM providers** — `src/sst/synthesizer.py` and `src/sst/gen.py`.
Add a new provider branch alongside the existing `anthropic` / `openai`
paths. Gate the import with `try/except ImportError`.

**Middleware integrations** — `src/sst/middleware.py` targets
Starlette/FastAPI today. Django, Flask, or gRPC integrations would
follow the same pattern: capture inputs, capture output, call
`_write_http_capture` (or construct `CapturePayload` directly).

**CLI commands** — `src/sst/cli.py` uses Click. New subcommands go
under the existing `baseline` group or as top-level commands.

**Diff policy** — `src/sst/diff.py`. New normalization rules go into
`normalize_for_compare`; new change types go into `build_structured_diff`.

## Code style

- Python 3.10+. No dependencies outside stdlib for the core module
  (`src/sst/core.py`) — `click` is the only runtime dependency.
- `starlette` is optional (`[fastapi]` extra).
- LLM SDKs are optional (`[llm]` extra).
- All new public functions need a docstring.
- All new code paths need a test.

## Pull request checklist

- [ ] `pytest` passes with no errors
- [ ] New feature has tests in `tests/`
- [ ] Optional dependencies are gated with `try/except ImportError`
- [ ] No new mandatory dependencies added to `[project.dependencies]`

## Code of conduct

Be respectful and professional in all interactions.
