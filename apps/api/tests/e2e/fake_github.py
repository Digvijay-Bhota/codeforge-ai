"""A fake GitHub REST API, served over real HTTP on loopback, for end-to-end tests.

CodeForge reaches GitHub through ``settings.github_api_url``.  Pointing that
setting at this server exercises the real token service (App JWT, scoped
installation tokens and their verification), the real ``GitHubClient`` and the
real repository-identity and collaborator checks, with only GitHub itself
replaced.  It implements the few endpoints the comment-to-approval workflow
uses, the way GitHub answers them, and records every request: anything else,
including every branch, push, pull-request or check-run call, is answered
404 and listed in ``unexpected``.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

BOT_LOGIN = "codeforge-e2e[bot]"


@dataclass
class Repo:
    id: int
    owner: str
    name: str
    installation_id: int
    head_sha: str
    default_branch: str = "main"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def json(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "full_name": self.full_name, "owner": {"login": self.owner},
            "default_branch": self.default_branch, "private": True,
            "clone_url": f"https://github.com/{self.full_name}.git", "html_url": f"https://github.com/{self.full_name}",
        }  # fmt: skip


@dataclass
class Recorded:
    method: str
    path: str
    auth: str  # "app-jwt", "installation-token", "none" or "invalid"
    body: Any = None


@dataclass
class Fault:
    """Answer requests matching *method* and the *path* regex with *status* instead of the real response.

    *times* limits how many requests it affects (None: all), as GitHub outages end.
    """

    method: str
    path: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    times: int | None = None
    hits: int = 0

    def matches(self, method: str, path: str) -> bool:
        return (
            method == self.method
            and re.fullmatch(self.path, path) is not None
            and (self.times is None or self.hits < self.times)
        )


@dataclass
class FakeGitHub:
    app_id: str
    public_key: str
    repos: dict[str, Repo] = field(default_factory=dict)
    # Collaborator permission per (repository full name, login); missing means not a collaborator.
    permissions: dict[tuple[str, str], str] = field(default_factory=dict)
    comments: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)
    unexpected: list[Recorded] = field(default_factory=list)
    # (installation id, repository full name, granted permissions) per minted token.
    minted: list[tuple[int, str, dict[str, str]]] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    _tokens: dict[str, tuple[int, str]] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(9_000_001))
    base_url: str = ""

    def add_repo(self, repo: Repo) -> None:
        self.repos[repo.full_name.lower()] = repo

    def writes(self) -> list[Recorded]:
        """Every request that changes something, other than CodeForge's own status comments."""
        comment_writes = ("/issues/", "/comments")
        return [
            r for r in self.requests
            if r.method in ("POST", "PUT", "PATCH", "DELETE")
            and not r.path.startswith("/app/installations/")
            and not all(part in r.path for part in comment_writes)
        ]  # fmt: skip

    # ── authentication ───────────────────────────────────────────────────────

    def _auth(self, request: Request) -> tuple[str, Any]:
        header = request.headers.get("authorization", "")
        scheme, _, credential = header.partition(" ")
        if not credential:
            return "none", None
        if credential in self._tokens:
            return "installation-token", self._tokens[credential]
        try:
            claims = jwt.decode(credential, self.public_key, algorithms=["RS256"])
        except jwt.PyJWTError:
            return "invalid", None
        return ("app-jwt", claims) if str(claims.get("iss")) == self.app_id else ("invalid", None)

    def _repo_for_token(self, request: Request, owner: str, name: str) -> Repo | JSONResponse:
        kind, grant = self._auth(request)
        repo = self.repos.get(f"{owner}/{name}".lower())
        if kind != "installation-token" or repo is None:
            return JSONResponse({"message": "Not Found"}, status_code=404)
        installation_id, granted_repo = grant
        if installation_id != repo.installation_id or granted_repo != repo.full_name:
            return JSONResponse({"message": "Not Found"}, status_code=404)  # token for another repository
        return repo

    # ── the application ──────────────────────────────────────────────────────

    def app(self) -> FastAPI:
        api = FastAPI()

        @api.middleware("http")
        async def record(request: Request, call_next: Any) -> Any:
            body = await request.body()
            kind, _ = self._auth(request)
            self.requests.append(Recorded(request.method, request.url.path, kind, body.decode() if body else None))
            for fault in self.faults:
                if fault.matches(request.method, request.url.path):
                    fault.hits += 1
                    return JSONResponse({"message": f"injected {fault.status}"}, status_code=fault.status,
                                        headers=fault.headers)  # fmt: skip
            return await call_next(request)

        @api.post("/app/installations/{installation_id}/access_tokens")
        async def access_token(installation_id: int, request: Request) -> JSONResponse:
            if self._auth(request)[0] != "app-jwt":
                return JSONResponse({"message": "A JSON web token could not be decoded"}, status_code=401)
            body = await request.json()
            installed = [r for r in self.repos.values() if r.installation_id == installation_id]
            if "repository_ids" in body:
                chosen = [r for r in installed if r.id in body["repository_ids"]]
            else:
                names = [n.lower() for n in body.get("repositories", [])]
                chosen = [r for r in installed if r.name.lower() in names]
            if len(chosen) != 1:
                return JSONResponse({"message": "There is at least one repository that does not exist"}, status_code=422)
            repo = chosen[0]
            token = f"ghs_e2e{next(self._ids)}"
            self._tokens[token] = (installation_id, repo.full_name)
            permissions = dict(body.get("permissions", {}))
            self.minted.append((installation_id, repo.full_name, permissions))
            return JSONResponse(
                {
                    "token": token,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    "permissions": permissions,
                    "repository_selection": "selected",
                    "repositories": [{"id": repo.id, "name": repo.name, "full_name": repo.full_name}],
                },
                status_code=201,
            )

        @api.get("/repos/{owner}/{name}")
        async def get_repo(owner: str, name: str, request: Request) -> Any:
            repo = self._repo_for_token(request, owner, name)
            return repo if isinstance(repo, JSONResponse) else repo.json()

        @api.get("/repos/{owner}/{name}/branches/{branch}")
        async def get_branch(owner: str, name: str, branch: str, request: Request) -> Any:
            repo = self._repo_for_token(request, owner, name)
            if isinstance(repo, JSONResponse):
                return repo
            if branch != repo.default_branch:
                return JSONResponse({"message": "Branch not found"}, status_code=404)
            return {"name": branch, "commit": {"sha": repo.head_sha}, "protected": False}

        @api.get("/repos/{owner}/{name}/collaborators/{login}/permission")
        async def permission(owner: str, name: str, login: str, request: Request) -> Any:
            repo = self._repo_for_token(request, owner, name)
            if isinstance(repo, JSONResponse):
                return repo
            level = self.permissions.get((repo.full_name, login))
            if level is None:
                return JSONResponse({"message": f"{login} is not a user"}, status_code=404)
            return {"permission": level, "role_name": level, "user": {"login": login}}

        @api.get("/repos/{owner}/{name}/issues/{number}/comments")
        async def list_comments(owner: str, name: str, number: int, request: Request) -> Any:
            repo = self._repo_for_token(request, owner, name)
            if isinstance(repo, JSONResponse):
                return repo
            return self.comments.get((repo.full_name, number), [])

        @api.post("/repos/{owner}/{name}/issues/{number}/comments")
        async def create_comment(owner: str, name: str, number: int, request: Request) -> Any:
            repo = self._repo_for_token(request, owner, name)
            if isinstance(repo, JSONResponse):
                return repo
            comment = self._comment((await request.json())["body"])
            self.comments.setdefault((repo.full_name, number), []).append(comment)
            return JSONResponse(comment, status_code=201)

        @api.patch("/repos/{owner}/{name}/issues/comments/{comment_id}")
        async def update_comment(owner: str, name: str, comment_id: int, request: Request) -> Any:
            repo = self._repo_for_token(request, owner, name)
            if isinstance(repo, JSONResponse):
                return repo
            for thread in self.comments.values():
                for comment in thread:
                    if comment["id"] == comment_id:
                        comment["body"] = (await request.json())["body"]
                        return comment
            return JSONResponse({"message": "Not Found"}, status_code=404)

        # Last: every other method and path (branches, pushes, pull requests,
        # check runs, contents ...).  Starlette prefers this full match over a
        # path that only matched with another method.
        @api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def anything_else(path: str, request: Request) -> JSONResponse:
            self.unexpected.append(self.requests[-1])
            return JSONResponse({"message": "Not Found"}, status_code=404)

        return api

    def _comment(self, body: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        cid = next(self._ids)
        return {
            "id": cid, "body": body, "html_url": f"https://github.com/comment/{cid}",
            "user": {"login": BOT_LOGIN, "id": 4242, "type": "Bot"}, "created_at": now, "updated_at": now,
        }  # fmt: skip

    def comment_bodies(self, full_name: str, number: int) -> list[str]:
        return [c["body"] for c in self.comments.get((full_name, number), [])]


class Served:
    """Runs a FakeGitHub on 127.0.0.1 (ephemeral port) in the current event loop."""

    def __init__(self, fake: FakeGitHub) -> None:
        self.fake = fake
        self.server = uvicorn.Server(
            uvicorn.Config(fake.app(), host="127.0.0.1", port=0, lifespan="off", log_config=None, access_log=False)
        )
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> FakeGitHub:
        self._task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.fake.base_url = f"http://127.0.0.1:{port}"
        return self.fake

    async def __aexit__(self, *exc: object) -> None:
        self.server.should_exit = True
        if self._task is not None:
            await self._task
