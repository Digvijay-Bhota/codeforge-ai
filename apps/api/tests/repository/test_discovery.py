from __future__ import annotations

from app.repository.discovery import (
    detect_language,
    is_important_file,
    is_test_file,
)


def test_detect_language() -> None:
    assert detect_language("foo.py") == "Python"
    assert detect_language("main.go") == "Go"
    assert detect_language("component.tsx") == "TypeScript"
    assert detect_language("Dockerfile") == "Dockerfile"
    assert detect_language("api.dockerfile") == "Dockerfile"
    assert detect_language("Makefile") == "Makefile"
    assert detect_language("unknown.xyz") is None

def test_is_test_file() -> None:
    assert is_test_file("tests/test_auth.py") is True
    assert is_test_file("test_utils.py") is True
    assert is_test_file("utils_test.py") is True
    assert is_test_file("app.test.ts") is True
    assert is_test_file("component.spec.jsx") is True
    assert is_test_file("main_test.go") is True

    assert is_test_file("main.py") is False
    assert is_test_file("test_data.json") is False
    assert is_test_file("app/tests/utils.py") is True # in tests dir

def test_is_important_file() -> None:
    assert is_important_file("README.md") is True
    assert is_important_file("pyproject.toml") is True
    assert is_important_file("package.json") is True
    assert is_important_file("main.py") is True
    assert is_important_file("src/main.ts") is True
    assert is_important_file("cmd/cli/main.go") is True

    assert is_important_file("utils.py") is False
    assert is_important_file("tests/test_main.py") is False
