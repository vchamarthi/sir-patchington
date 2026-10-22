"""Credential-bearing GitHub boundary. No repository code is executed here."""

from __future__ import annotations

import base64
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from patchington.contracts import (
    Candidate,
    ChangedFile,
    ChangeSnapshot,
    Finding,
    RepoPolicy,
    ReviewThread,
)

API_VERSION = "2026-03-10"
API_URL = "https://api.github.com"


class GitHubError(RuntimeError):
    """A bounded, secret-free GitHub boundary failure."""


class GitHubTransientError(GitHubError):
    """Network, rate-limit or server failure eligible for bounded reconciliation/retry."""


class GitOperationError(GitHubError):
    """A Git subprocess failed without exposing possibly credential-bearing stderr."""


def _positive_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("GitHub identifiers must be positive integers")


def _json_response(response: httpx.Response) -> Any:
    if not 200 <= response.status_code < 300:
        # Error bodies may reflect repository secrets or headers; never propagate them.
        if response.status_code == 429 or 500 <= response.status_code < 600:
            raise GitHubTransientError(f"GitHub returned transient status {response.status_code}")
        raise GitHubError(f"GitHub returned status {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubError("GitHub returned invalid JSON") from exc


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION, "User-Agent": "Sir-Patchington/0.1"}


def _literal(value: str) -> str:
    """CommonMark code spans suppress model-authored images, HTML, links and mentions."""
    value = " ".join(value.split())
    delimiter = "`" * (max((len(run) for run in re.findall(r"`+", value)), default=0) + 1)
    return delimiter + " " + value + " " + delimiter


def _review_limitations_section(limitations: tuple[str, ...]) -> str:
    if not limitations:
        return ""
    if not isinstance(limitations, tuple) or any(not isinstance(value, str) for value in limitations):
        raise GitHubError("Invalid review limitations")
    rows, used, omitted = [], 0, len(limitations) > 20
    for limitation in limitations[:20]:
        data = limitation.encode()
        omitted = omitted or len(data) > 1_000
        row = "- " + _literal(data[:1_000].decode("utf-8", errors="ignore")) + "\n"
        if used + len(row.encode()) > 10_000:
            omitted = True
            break
        rows.append(row)
        used += len(row.encode())
    if omitted:
        rows.append("Additional review limitation text omitted by publication bounds.\n")
    return ("\n**Review coverage is limited.** Findings apply only to the evidence available "
            "during this review.\n\n<details><summary>Review limitations</summary>\n\n"
            + "".join(rows) + "\n</details>\n")


class AppTokenProvider:
    """Mint a new single-repository token per request; never reuse a broader scope."""

    def __init__(self, app_id: int, key_pem: bytes | str,
                 transport: httpx.BaseTransport | None = None):
        _positive_id(app_id)
        self.app_id = app_id
        self._key = key_pem
        self._http = httpx.Client(transport=transport, follow_redirects=False,
                                 timeout=30, trust_env=False)

    def __call__(self, installation_id: int, repository_id: int, write: bool = False) -> str:
        permissions = ({"contents": "write", "metadata": "read"} if write else
                       {"contents": "read", "pull_requests": "read", "checks": "read",
                        "statuses": "read", "metadata": "read"})
        return self._mint(installation_id, repository_id, permissions)

    def review_token(self, installation_id: int, repository_id: int) -> str:
        return self._mint(installation_id, repository_id,
                          {"pull_requests": "write", "metadata": "read"})

    def ci_token(self, installation_id: int, repository_id: int) -> str:
        return self._mint(installation_id, repository_id,
                          {"actions": "read", "checks": "read", "statuses": "read",
                           "metadata": "read"})

    def _mint(self, installation_id: int, repository_id: int,
              permissions: dict[str, str]) -> str:
        _positive_id(installation_id)
        _positive_id(repository_id)
        now = int(time.time())
        token = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)},
                           self._key, algorithm="RS256")
        try:
            response = self._http.post(
                f"{API_URL}/app/installations/{installation_id}/access_tokens",
                headers=_headers(token),
                json={"repository_ids": [repository_id], "permissions": permissions})
        except httpx.TransportError as exc:
            raise GitHubTransientError("Installation token request temporarily unavailable") from exc
        except httpx.HTTPError as exc:
            raise GitHubError("Installation token request failed") from exc
        body = _json_response(response)
        if not isinstance(body, dict) or not isinstance(body.get("token"), str) or not body["token"]:
            raise GitHubError("Missing installation token")
        return body["token"]


TokenProvider = Callable[[int, int, bool], str]


class SplitTokenProvider:
    """Map enrolled read-App installations to separate write-App installations."""

    def __init__(self, read: AppTokenProvider, write: AppTokenProvider,
                 installation_map: dict[tuple[int, int], int]):
        for (installation, repository), target in installation_map.items():
            _positive_id(installation)
            _positive_id(repository)
            _positive_id(target)
        self._read, self._write = read, write
        self._map = dict(installation_map)

    @property
    def app_id(self) -> int:
        return self._write.app_id

    def _installation(self, installation_id: int, repository_id: int) -> int:
        try:
            return self._map[(installation_id, repository_id)]
        except KeyError as exc:
            raise GitHubError("No authorized write-App installation mapping") from exc

    def __call__(self, installation_id: int, repository_id: int, write: bool = False) -> str:
        if write:
            return self._write(self._installation(installation_id, repository_id), repository_id, True)
        return self._read(installation_id, repository_id, False)

    def review_token(self, installation_id: int, repository_id: int) -> str:
        return self._write.review_token(self._installation(installation_id, repository_id),
                                        repository_id)

    def ci_token(self, installation_id: int, repository_id: int) -> str:
        return self._read.ci_token(installation_id, repository_id)


_THREAD_QUERY = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){
 repository(owner:$owner,name:$name){databaseId pullRequest(number:$number){
 reviewThreads(first:100,after:$cursor){nodes{id path line isResolved isOutdated
 comments(first:100){nodes{id body author{login} updatedAt}
 pageInfo{hasNextPage endCursor}}} pageInfo{hasNextPage endCursor}}}}}"""
_COMMENTS_QUERY = """query($thread:ID!,$cursor:String){node(id:$thread){
 ... on PullRequestReviewThread{comments(first:100,after:$cursor){
 nodes{id body author{login} updatedAt} pageInfo{hasNextPage endCursor}}}}}"""


def git_environment(home: Path, token: str | None = None) -> dict[str, str]:
    """A fresh environment, never a filtered copy containing caller secrets/config."""
    env = {"PATH": os.defpath, "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_LFS_SKIP_SMUDGE": "1"}
    if token is not None:
        value = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {value}"})
    return env


def run_git(path: Path, args: list[str], *, token: str | None = None,
            timeout: int = 120) -> str:
    """Run trusted Git arguments only; errors never include credentials or stderr."""
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise GitHubError("Git executable unavailable")
    with tempfile.TemporaryDirectory(prefix="patchington-git-home-") as home:
        from patchington.gitwork import bounded_run
        result = bounded_run(
                [executable, "-c", "core.hooksPath=" + os.devnull,
                 "-c", "credential.helper=", "-c", "http.followRedirects=false",
                 "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                 "-c", "protocol.file.allow=never", "-c", "submodule.recurse=false",
                 "-c", "core.fsmonitor=false", "-c", "maintenance.auto=false",
                 "-c", "gc.auto=0", *args], cwd=path, limit=1_000_000,
                timeout=timeout, env=git_environment(Path(home), token))
    if result.code != 0 or result.timed_out or result.overflow:
        raise GitOperationError("Git operation rejected")
    try:
        return result.output.decode("utf-8").strip()
    except UnicodeError as exc:
        raise GitHubError("Invalid Git response encoding") from exc


class GitHubClient:
    def __init__(self, token_provider: TokenProvider,
                 transport: httpx.BaseTransport | None = None, api_url: str = API_URL):
        # Enterprise hosts need their own explicitly qualified transport/clone mapping.
        if api_url.rstrip("/") != API_URL:
            raise ValueError("Only the qualified api.github.com endpoint is supported")
        self._tokens = token_provider
        self._transport = transport
        self._http = httpx.Client(transport=transport, follow_redirects=False,
                                 timeout=30, trust_env=False)

    @staticmethod
    def _remote(policy: RepoPolicy) -> str:
        return f"https://github.com/{policy.repo}.git"

    @staticmethod
    def _git(path: Path, args: list[str], **kwargs: Any) -> str:
        return run_git(path, args, **kwargs)

    def _request(self, policy: RepoPolicy, method: str, path: str,
                 *, write: bool = False, guard: Callable[[], None] | None = None,
                 ci: bool = False, response_limit: int = 10_000_000,
                 expect_json: bool = True, deadline: float | None = None,
                 **kwargs: Any) -> httpx.Response:
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise GitHubError("Unsafe API path")
        review_token = getattr(self._tokens, "review_token", None)
        ci_token = getattr(self._tokens, "ci_token", None)
        if ci and (write or method != "GET"):
            raise GitHubError("CI access is read only")
        if ci and ci_token:
            token = ci_token(policy.installation_id, policy.repository_id)
        else:
            token = (review_token(policy.installation_id, policy.repository_id)
                     if write and review_token else
                     self._tokens(policy.installation_id, policy.repository_id, write))
        if write and guard:
            guard()
        if deadline is not None and time.monotonic() >= deadline:
            raise GitHubError("GitHub request time budget exhausted")
        try:
            with self._http.stream(method, API_URL + path, headers=_headers(token), **kwargs) as raw:
                content = bytearray()
                for block in raw.iter_bytes():
                    if deadline is not None and time.monotonic() >= deadline:
                        raise GitHubError("GitHub response time budget exhausted")
                    if len(content) + len(block) > response_limit:
                        raise GitHubError("GitHub response exceeds bound")
                    content.extend(block)
                decoded_headers = {key: value for key, value in raw.headers.items()
                                   if key.lower() not in {"content-encoding", "content-length"}}
                response = httpx.Response(raw.status_code, headers=decoded_headers,
                                          content=bytes(content), request=raw.request)
        except httpx.TransportError as exc:
            raise GitHubTransientError("GitHub request temporarily unavailable") from exc
        except httpx.HTTPError as exc:
            raise GitHubError("GitHub request failed") from exc
        if expect_json:
            _json_response(response)
        elif response.status_code != 302 and not 200 <= response.status_code < 300:
            raise GitHubError(f"GitHub returned status {response.status_code}")
        return response

    def _get(self, policy: RepoPolicy, path: str) -> Any:
        return self._request(policy, "GET", path).json()

    def _pages(self, policy: RepoPolicy, path: str, *, key: str | None = None,
               limit: int = 10_000) -> list[dict[str, Any]]:
        initial = httpx.URL(API_URL + path)
        paths = {initial.path, initial.path.replace(f"/repos/{policy.repo}/",
                                                  f"/repositories/{policy.repository_id}/", 1)}
        current = str(initial)
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for _ in range(100):
            url = httpx.URL(current)
            if (url.scheme != "https" or url.host != "api.github.com" or url.port not in (None, 443)
                    or url.path not in paths or url.userinfo
                    or set(url.params) - {"page", "per_page", "filter", "state"}):
                raise GitHubError("Unsafe pagination URL")
            if current in seen:
                raise GitHubError("Cyclic pagination")
            seen.add(current)
            response = self._request(policy, "GET", url.raw_path.decode())
            body = response.json()
            rows = body.get(key) if key and isinstance(body, dict) else body
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise GitHubError("Malformed paginated response")
            result.extend(rows)
            if len(result) > limit:
                raise GitHubError("Pagination exceeds policy bound")
            next_link = response.links.get("next", {}).get("url")
            if not next_link:
                return result
            current = next_link
        raise GitHubError("Pagination exceeds page bound")

    def list_open_prs(self, policy: RepoPolicy) -> list[int]:
        """Discover at most 1,000 eligible PR IDs; later reconciliation captures their heads."""
        repo = self._get(policy, f"/repos/{policy.repo}")
        if repo.get("id") != policy.repository_id or repo.get("full_name", "").lower() != policy.repo.lower():
            raise GitHubError("Repository identity mismatch during PR discovery")
        rows = self._pages(policy, f"/repos/{policy.repo}/pulls?state=open&per_page=100", limit=1000)
        result, seen = [], set()
        for row in rows:
            try:
                number = row["number"]
                _positive_id(number)
                base = row["base"]["repo"]
                head = row["head"]["repo"]
                if (base["id"] != policy.repository_id
                        or base["full_name"].lower() != policy.repo.lower()
                        or row["state"] != "open" or number in seen):
                    raise GitHubError("PR discovery identity mismatch or duplicate")
                seen.add(number)
                if head is None:
                    continue
                if row.get("draft", True) and not policy.allow_drafts:
                    continue
                if head["id"] != policy.repository_id and not policy.allow_forks:
                    continue
                if head["id"] == policy.repository_id and head["full_name"].lower() != policy.repo.lower():
                    raise GitHubError("PR head repository identity mismatch")
                result.append(number)
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError("Malformed PR discovery identity") from exc
        return result

    def _pull(self, policy: RepoPolicy, pr: int) -> dict[str, Any]:
        _positive_id(pr)
        body = self._get(policy, f"/repos/{policy.repo}/pulls/{pr}")
        try:
            base_repo, head_repo = body["base"]["repo"], body["head"]["repo"]
            if (body["number"] != pr or base_repo["id"] != policy.repository_id
                    or base_repo["full_name"].lower() != policy.repo.lower()):
                raise GitHubError("PR repository identity mismatch")
            if body["state"] != "open" or body.get("merged"):
                raise GitHubError("PR is not open")
            if body.get("draft", True) and not policy.allow_drafts:
                raise GitHubError("Draft PR is disallowed")
            if head_repo["id"] != policy.repository_id and not policy.allow_forks:
                raise GitHubError("Fork PR is disallowed")
            if body["changed_files"] > policy.max_files:
                raise GitHubError("Changed files exceed policy bound")
        except (KeyError, TypeError) as exc:
            raise GitHubError("Malformed PR identity") from exc
        return body

    def _graphql(self, policy: RepoPolicy, query: str, variables: dict) -> dict:
        result = self._request(policy, "POST", "/graphql", json={"query": query,
                               "variables": variables}).json()
        if result.get("errors") or not isinstance(result.get("data"), dict):
            raise GitHubError("Incomplete review-thread response")
        return result["data"]

    @staticmethod
    def _cursor(connection: dict, seen: set[str]) -> str | None:
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            return None
        cursor = page["endCursor"]
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise GitHubError("Invalid GraphQL pagination cursor")
        seen.add(cursor)
        return cursor

    def _threads(self, policy: RepoPolicy, pr: int) -> tuple[ReviewThread, ...]:
        owner, name = policy.repo.split("/")
        cursor = None
        seen: set[str] = set()
        threads = []
        total_bytes = 0
        for _ in range(100):
            data = self._graphql(policy, _THREAD_QUERY,
                                 {"owner": owner, "name": name, "number": pr, "cursor": cursor})
            try:
                repository = data["repository"]
                if repository["databaseId"] != policy.repository_id:
                    raise GitHubError("Review repository identity mismatch")
                connection = repository["pullRequest"]["reviewThreads"]
                for node in connection["nodes"]:
                    comments = list(node["comments"]["nodes"])
                    comment_seen: set[str] = set()
                    comment_cursor = self._cursor(node["comments"], comment_seen)
                    for _page in range(100):
                        if comment_cursor is None:
                            break
                        more = self._graphql(policy, _COMMENTS_QUERY,
                                             {"thread": node["id"], "cursor": comment_cursor})
                        nested = more["node"]["comments"]
                        comments.extend(nested["nodes"])
                        comment_cursor = self._cursor(nested, comment_seen)
                    if comment_cursor is not None or len(comments) > 10_000:
                        raise GitHubError("Review comments exceed bound")
                    body = "\n\n".join(f"[{c['id']}] {(c.get('author') or {}).get('login', 'deleted')}: "
                                          + c["body"] for c in comments)
                    total_bytes += len(body.encode())
                    if total_bytes > policy.max_source_bytes or len(threads) >= 10_000:
                        raise GitHubError("Review context exceeds bound")
                    threads.append(ReviewThread(id=node["id"], body=body,
                        author=(comments[0].get("author") or {}).get("login", "deleted")
                               if comments else "deleted", path=node.get("path"),
                        line=node.get("line"), resolved=node["isResolved"],
                        outdated=node["isOutdated"],
                        updated_at=max((c["updatedAt"] for c in comments), default="")))
                cursor = self._cursor(connection, seen)
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError("Malformed review-thread response") from exc
            if cursor is None:
                return tuple(threads)
        raise GitHubError("Review threads exceed page bound")

    def _review_summaries(self, policy: RepoPolicy, pr: int, head_sha: str,
                          inline: tuple[ReviewThread, ...]) -> tuple[ReviewThread, ...]:
        rows = self._pages(policy, f"/repos/{policy.repo}/pulls/{pr}/reviews?per_page=100")
        result = list(inline)
        total = sum(len(thread.body.encode()) for thread in inline)
        seen = set()
        for row in rows:
            try:
                review_id = row["id"]
                _positive_id(review_id)
                if review_id in seen:
                    raise GitHubError("Duplicate review summary identity")
                seen.add(review_id)
                user = row.get("user") or {}
                if user.get("type") == "Bot":
                    continue
                state = row.get("state", "UNKNOWN")
                text = row.get("body") or ""
                if not isinstance(state, str) or not isinstance(text, str):
                    raise GitHubError("Malformed review summary text")
                if state.upper() == "PENDING" and not row.get("submitted_at"):
                    continue
                body = f"[GitHub review state: {state}]\n{text}"
                total += len(body.encode())
                if total > policy.max_source_bytes or len(result) >= 10_000:
                    raise GitHubError("Combined review feedback exceeds policy bound")
                result.append(ReviewThread(id=f"review:{review_id}", body=body,
                    author=user.get("login") or "unknown", path=None, line=None,
                    resolved=state.upper() == "DISMISSED",
                    outdated=row.get("commit_id") != head_sha,
                    updated_at=row.get("submitted_at") or ""))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise GitHubError("Malformed review summary response") from exc
        return tuple(result)

    @staticmethod
    def _pull_identity(body: dict) -> tuple:
        return (body["base"]["sha"], body["head"]["sha"], body["base"]["ref"],
                body["head"]["ref"], body["head"]["repo"]["full_name"],
                body.get("title"), body.get("body"), body["changed_files"])

    def snapshot(self, policy: RepoPolicy, pr: int) -> ChangeSnapshot:
        repo = self._get(policy, f"/repos/{policy.repo}")
        if repo.get("id") != policy.repository_id or repo.get("full_name", "").lower() != policy.repo.lower():
            raise GitHubError("Repository identity mismatch")
        first = self._pull(policy, pr)
        files = self._pages(policy, f"/repos/{policy.repo}/pulls/{pr}/files?per_page=100",
                            limit=policy.max_files)
        if len(files) != first["changed_files"]:
            raise GitHubError("Incomplete PR file listing")
        if sum(len(f.get("patch", "").encode()) for f in files) > policy.max_source_bytes:
            raise GitHubError("PR patch context exceeds policy bound")
        threads = self._review_summaries(policy, pr, first["head"]["sha"], self._threads(policy, pr))
        last = self._pull(policy, pr)
        if self._pull_identity(first) != self._pull_identity(last):
            raise GitHubError("PR changed during snapshot capture")
        try:
            return ChangeSnapshot(tenant=policy.tenant, repository_id=policy.repository_id,
                installation_id=policy.installation_id, repo=policy.repo, pr=pr,
                base_sha=last["base"]["sha"], head_sha=last["head"]["sha"],
                base_ref=last["base"]["ref"], head_ref=last["head"]["ref"],
                head_repo=last["head"]["repo"]["full_name"],
                is_fork=last["head"]["repo"]["id"] != policy.repository_id,
                title=last.get("title", ""), body=last.get("body") or "", threads=threads,
                files=tuple(ChangedFile(path=f["filename"], status=f["status"],
                    patch=f.get("patch", ""), previous_path=f.get("previous_filename")) for f in files))
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("Malformed PR snapshot") from exc

    @staticmethod
    def _scope(policy: RepoPolicy, snapshot: ChangeSnapshot) -> None:
        if (snapshot.tenant != policy.tenant or snapshot.repo != policy.repo
                or snapshot.repository_id != policy.repository_id
                or snapshot.installation_id != policy.installation_id):
            raise GitHubError("Snapshot is outside repository policy")

    def fetch(self, policy: RepoPolicy, snapshot: ChangeSnapshot, destination: Path):
        from patchington.gitwork import GitWorkspace
        self._scope(policy, snapshot)
        if destination.exists():
            raise GitHubError("Fetch destination already exists")
        destination.mkdir(mode=0o700, parents=True)
        self._git(destination, ["init", "--quiet", "--template="])
        token = self._tokens(policy.installation_id, policy.repository_id, False)
        remote = self._remote(policy)
        for revision in dict.fromkeys((snapshot.base_sha, snapshot.head_sha)):
            self._git(destination, ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
                                  "--depth=1", "--", remote, revision], token=token)
            actual = self._git(destination, ["rev-parse", "--verify", revision + "^{commit}"])
            if actual != revision:
                raise GitHubError("Fetched commit identity mismatch")
        return GitWorkspace(destination)

    @staticmethod
    def _anchor(snapshot: ChangeSnapshot, finding: Finding) -> bool:
        file = next((f for f in snapshot.files if f.path == finding.path), None)
        if file is None:
            return False
        line = None
        for row in file.patch.splitlines():
            match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
            if match:
                line = int(match[1])
            elif line is not None and row.startswith(("+", " ")):
                if line == finding.line:
                    return True
                line += 1
        return False

    def post_review(self, policy: RepoPolicy, snapshot: ChangeSnapshot,
                    findings: tuple[Finding, ...], *, guard: Callable[[], None] | None = None,
                    ci_report: dict | None = None,
                    review_limitations: tuple[str, ...] = ()) -> str:
        self._scope(policy, snapshot)
        if ci_report is not None and (ci_report.get("head_sha") != snapshot.head_sha
                or ci_report.get("repository_id", policy.repository_id) != policy.repository_id
                or ci_report.get("repo", policy.repo) != policy.repo):
            raise GitHubError("CI report belongs to another repository or commit")
        if not policy.post_reviews:
            raise GitHubError("Review posting is disabled")
        if len(findings) > policy.max_findings:
            raise GitHubError("Findings exceed policy bound")
        if any(f.status != "supported" or not self._anchor(snapshot, f) for f in findings):
            raise GitHubError("Finding lacks a supported current diff anchor")
        app_id = getattr(self._tokens, "app_id", None)
        if app_id is None:
            raise GitHubError("Review reconciliation requires an authenticated App identity")
        current = self._pull(policy, snapshot.pr)
        if current["head"]["sha"] != snapshot.head_sha or current["base"]["sha"] != snapshot.base_sha:
            raise GitHubError("Stale review snapshot")
        marker = f"<!-- sir-patchington:review:v1:{policy.repository_id}:{snapshot.pr} -->"
        body = marker + f"\nReviewed commit `{snapshot.head_sha}`.\n"
        body += _review_limitations_section(review_limitations)
        for finding in sorted(findings, key=lambda f: (f.path, f.line, f.fingerprint)):
            anchor = (f"https://github.com/{policy.repo}/blob/{snapshot.head_sha}/"
                      f"{quote(finding.path, safe='/')}#L{finding.line}")
            body += (f"\n- **{finding.severity}** [source]({anchor}) "
                     f"{_literal(finding.path)} line {finding.line}: {_literal(finding.claim)}"
                     f"\n  Trigger: {_literal(finding.trigger)}\n  Expected: {_literal(finding.expected)}"
                     f"\n  Evidence: {_literal(finding.reason)}\n")
        if ci_report is not None:
            from patchington.ci import render_ci_summary
            body += "\n" + render_ci_summary(ci_report) + "\n"
        if len(body.encode()) > 60_000:
            raise GitHubError("Review body exceeds publication bound")
        comments = self._pages(policy, f"/repos/{policy.repo}/issues/{snapshot.pr}/comments?per_page=100")
        owned = [c for c in comments if c.get("body", "").startswith(marker)
                 and (c.get("performed_via_github_app") or {}).get("id") == app_id
                 and (c.get("user") or {}).get("type") == "Bot"]
        if len(owned) > 1:
            raise GitHubError("Duplicate owned review markers require reconciliation")
        if owned and owned[0]["body"] == body:
            return str(owned[0]["id"])
        latest = self._pull(policy, snapshot.pr)
        if latest["head"]["sha"] != snapshot.head_sha or latest["base"]["sha"] != snapshot.base_sha:
            raise GitHubError("Stale review snapshot at publication boundary")
        if owned:
            path, method = f"/repos/{policy.repo}/issues/comments/{int(owned[0]['id'])}", "PATCH"
        else:
            path, method = f"/repos/{policy.repo}/issues/{snapshot.pr}/comments", "POST"
        # A transport failure is ambiguous: do not retry blindly; next invocation re-lists markers.
        result = self._request(policy, method, path, write=True, guard=guard, json={"body": body}).json()
        return str(result["id"])

    def checks(self, policy: RepoPolicy, snapshot: ChangeSnapshot) -> dict:
        self._scope(policy, snapshot)
        sha = snapshot.head_sha
        runs = self._pages(policy, f"/repos/{policy.repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                           key="check_runs")
        statuses = self._pages(policy, f"/repos/{policy.repo}/commits/{sha}/statuses?per_page=100")
        if any(r.get("head_sha") != sha for r in runs):
            raise GitHubError("Check run belongs to another commit")
        return {"head_sha": sha, "check_runs": runs, "statuses": statuses}

    def ci_review(self, policy: RepoPolicy, snapshot: ChangeSnapshot) -> dict:
        """Observe CI for an exact commit without executing repository code or changing runs."""
        from patchington.ci import collect_ci
        self._scope(policy, snapshot)
        return collect_ci(self, policy, snapshot)

    def _remote_refs(self, policy: RepoPolicy, snapshot: ChangeSnapshot, path: Path) -> dict:
        token = self._tokens(policy.installation_id, policy.repository_id, False)
        output = self._git(path, ["ls-remote", "--refs", "--", self._remote(policy),
                           f"refs/heads/{snapshot.base_ref}", f"refs/heads/{snapshot.head_ref}"],
                           token=token)
        refs = {}
        for row in output.splitlines():
            try:
                sha, ref = row.split("\t")
            except ValueError as exc:
                raise GitHubError("Malformed remote refs") from exc
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise GitHubError("Malformed remote commit identity")
            refs[ref] = sha
        return refs

    def publish_candidate(self, policy: RepoPolicy, snapshot: ChangeSnapshot,
                          candidate: Candidate, workspace, *, reconcile_only: bool = False,
                          guard: Callable[[], None] | None = None) -> dict:
        """Only Publisher may call this with an independently reconstructed candidate."""
        self._scope(policy, snapshot)
        if (not policy.publish_enabled or snapshot.is_fork or snapshot.head_repo != policy.repo
                or snapshot.head_ref in {snapshot.base_ref, policy.default_branch}):
            raise GitHubError("Publication is disabled or target is ineligible")
        live = self.snapshot(policy, snapshot.pr)
        if live.head_sha != candidate.commit_sha and live.identity != snapshot.identity:
            raise GitHubError("PR changed at the publication boundary")
        if (live.base_sha != snapshot.base_sha or live.base_ref != snapshot.base_ref
                or live.head_ref != snapshot.head_ref or live.head_repo != snapshot.head_repo):
            raise GitHubError("PR branch identity changed at the publication boundary")
        base_ref, head_ref = f"refs/heads/{snapshot.base_ref}", f"refs/heads/{snapshot.head_ref}"
        before = self._remote_refs(policy, snapshot, workspace.path)
        if before.get(base_ref) != snapshot.base_sha:
            raise GitHubError("PR base changed before publication")
        reconciled = before.get(head_ref) == candidate.commit_sha
        push_failed = False
        if not reconciled:
            if reconcile_only or before.get(head_ref) != snapshot.head_sha:
                raise GitHubError("PR head changed before publication")
            token = self._tokens(policy.installation_id, policy.repository_id, True)
            if guard:
                guard()
            try:
                self._git(workspace.path, ["push", "--porcelain", "--atomic", "--no-verify",
                    "--no-follow-tags", "--recurse-submodules=no",
                    f"--force-with-lease={head_ref}:{snapshot.head_sha}", "--",
                    self._remote(policy), f"{candidate.commit_sha}:{head_ref}"], token=token)
            except GitHubError:
                # A lost response may follow a successful server transaction. Read before retrying.
                reconciled = True
                push_failed = True
        try:
            after = self._remote_refs(policy, snapshot, workspace.path)
        except GitOperationError as exc:
            raise GitHubTransientError("Publication outcome uncertain; remote ref readback unavailable") from exc
        if after.get(head_ref) != candidate.commit_sha:
            if (push_failed and after.get(head_ref) == snapshot.head_sha
                    and after.get(base_ref) == snapshot.base_sha):
                raise GitHubTransientError("Git push failed with tested refs unchanged; bounded retry is safe")
            raise GitHubError("Publication rejected or remote head changed; reconcile before retry")
        commit = self._get(policy, f"/repos/{policy.repo}/git/commits/{candidate.commit_sha}")
        if (commit.get("sha") != candidate.commit_sha
                or (commit.get("tree") or {}).get("sha") != candidate.tree_sha
                or [p.get("sha") for p in commit.get("parents", [])] != [candidate.parent_sha]):
            raise GitHubError("Remote commit/tree/parent readback mismatch; publication outcome uncertain")
        if after.get(base_ref) != snapshot.base_sha:
            # The base branch is independently mutable; never imply an atomic two-branch lock.
            return {"status": "published_stale_base", "commit_sha": candidate.commit_sha,
                    "tree_sha": candidate.tree_sha, "requires_reverification": True}
        return {"status": "reconciled" if reconciled else "published",
                "commit_sha": candidate.commit_sha, "tree_sha": candidate.tree_sha,
                "requires_reverification": False}
