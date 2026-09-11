from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from app.workspace.manager import WorkspaceManager

LANGUAGE_EXTENSIONS = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".php": "PHP",
    ".rb": "Ruby",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".swift": "Swift",
    ".sh": "Shell",
    ".bash": "Shell",
}

IMPORTANT_FILES_EXACT = frozenset({
    "readme.md",
    "readme",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "tsconfig.json",
    "dockerfile",
    "docker-compose.yml",
    "makefile",
    "go.mod",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
})

def detect_language(path: str) -> str | None:
    p = Path(path)
    name = p.name.lower()
    if name == "dockerfile" or name.endswith(".dockerfile"):
        return "Dockerfile"
    if name == "makefile":
        return "Makefile"
    return LANGUAGE_EXTENSIONS.get(p.suffix.lower())

def is_test_file(path: str) -> bool:
    """Determine if a file is likely a test file."""
    p = Path(path)
    name = p.name.lower()

    # Look for 'tests' or 'test' in directory components
    if any(part.lower() in ("tests", "test") for part in p.parts[:-1]):
        if name.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".cpp", ".c", ".cs", ".php", ".rb")):
            return True

    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    if name.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts", ".test.jsx", ".test.tsx", ".spec.jsx", ".spec.tsx")):
        return True
    if name.endswith("_test.go"):
        return True

    return False

def is_important_file(path: str) -> bool:
    """Determine if a file is high-value for understanding the project."""
    p = Path(path)
    name = p.name.lower()

    if name in IMPORTANT_FILES_EXACT:
        return True

    parts = p.parts
    # Entrypoints at root or one level deep
    if len(parts) <= 2:
        if name in ("main.py", "app.py", "server.py", "index.js", "index.ts", "main.ts", "main.go"):
            return True

    # Go cmd entrypoints
    if name == "main.go" and "cmd" in parts:
        return True

    return False

def detect_frameworks_and_package_managers(workspace: WorkspaceManager) -> tuple[list[str], list[str]]:
    frameworks = set()
    pms = set()

    with suppress(Exception):
        content = workspace.read_file("pyproject.toml").lower()
        pms.add("poetry/pip")
        if "fastapi" in content:
            frameworks.add("FastAPI")
        if "django" in content:
            frameworks.add("Django")
        if "flask" in content:
            frameworks.add("Flask")
        if "pytest" in content:
            frameworks.add("pytest")

    with suppress(Exception):
        content = workspace.read_file("requirements.txt").lower()
        pms.add("pip")
        if "fastapi" in content:
            frameworks.add("FastAPI")
        if "django" in content:
            frameworks.add("Django")
        if "flask" in content:
            frameworks.add("Flask")
        if "pytest" in content:
            frameworks.add("pytest")

    with suppress(Exception):
        content = workspace.read_file("package.json").lower()
        pms.add("npm/yarn/pnpm")
        if "next" in content:
            frameworks.add("Next.js")
        if "react" in content:
            frameworks.add("React")
        if "express" in content:
            frameworks.add("Express")
        if "nestjs" in content:
            frameworks.add("NestJS")
        if "vue" in content:
            frameworks.add("Vue")

    with suppress(Exception):
        workspace.read_file("go.mod")
        pms.add("go modules")

    with suppress(Exception):
        workspace.read_file("Cargo.toml")
        pms.add("cargo")

    with suppress(Exception):
        workspace.read_file("pom.xml")
        pms.add("maven")

    with suppress(Exception):
        workspace.read_file("build.gradle")
        pms.add("gradle")

    return sorted(list(frameworks)), sorted(list(pms))
