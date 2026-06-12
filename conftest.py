"""Root conftest: makes the repo root importable so test modules can use
`from tests.conftest import Seed` under any pytest invocation (CI runs plain
`uv run pytest`; pytest inserts this file's directory into sys.path)."""
