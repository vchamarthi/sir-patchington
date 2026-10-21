"""Complete offline control-plane qualification with real Docker and local Git.

Only the fixture repository is publish-enabled. Model generation, GitHub metadata
and CI are explicitly simulated; no external model or GitHub request is made.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from pathlib import Path

import httpx

from patchington.api import Principal, create_app
from patchington.artifacts import ArtifactStore
from patchington.contracts import Candidate, ChangedFile, ChangeSnapshot, RepoPolicy
from patchington.execution import DockerVerifier
from patchington.github import GitHubClient, GitHubError, run_git
from patchington.models import FixtureModel
from patchington.pipeline import Pipeline
from patchington.probes import delivery_fee_probe
from patchington.publisher import Publisher
from patchington.store import Store

DEFAULT_IMAGE = "python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
_BASE = "def delivery_fee(subtotal):\n    return 0 if subtotal >= 50 else 5\n"
_HEAD = "def delivery_fee(subtotal):\n    return 0 if subtotal > 50 else 5\n"
_FINDING = {"path": "src/fees.py", "line": 2, "severity": "high", "category": "correctness",
            "claim": "Delivery fee excludes the specified fifty boundary",
            "trigger": "subtotal=50", "expected": "0",
            "reason": "The owner contract requires free delivery at fifty or above."}


def _fixture_git(path: Path, args: list[str]) -> str:
    # This file transport is confined to explicitly generated local demo repositories.
    return run_git(path, ["-c", "protocol.file.allow=always", "-c", "user.name=Demo Fixture",
                          "-c", "user.email=demo@localhost", *args])


class LocalForge(GitHubClient):
    """Simulated PR metadata with actual shared fetch/CAS/readback implementation."""

    def __init__(self, remote: Path):
        super().__init__(lambda installation, repository, write=False: "offline-fixture-token")
        self.remote = remote.resolve()
        self.reviews: list[dict] = []
        self.ci_observations: list[dict] = []

    def _remote(self, policy):
        return str(self.remote)

    @staticmethod
    def _git(path, args, **kwargs):
        return _fixture_git(path, args)

    def snapshot(self, policy: RepoPolicy, pr: int) -> ChangeSnapshot:
        if pr != 17:
            raise GitHubError("only the controlled fixture PR is available")
        base = self._git(self.remote, ["rev-parse", "refs/heads/main"])
        head = self._git(self.remote, ["rev-parse", "refs/heads/feature"])
        diff = self._git(self.remote, ["diff", "--no-ext-diff", "--no-textconv", base, head,
                                      "--", "src/fees.py"])
        return ChangeSnapshot(tenant=policy.tenant, repository_id=policy.repository_id,
                              installation_id=policy.installation_id, repo=policy.repo, pr=pr,
                              base_sha=base, head_sha=head, base_ref="main", head_ref="feature",
                              head_repo=policy.repo, title="Offline delivery-fee boundary fixture",
                              body="Demonstration only: delivery is free at subtotal 50 or above.",
                              files=(ChangedFile(path="src/fees.py", patch=diff + "\n"),) if diff else ())

    def post_review(self, policy, snapshot, findings, *, guard=None):
        self._scope(policy, snapshot)
        if not policy.post_reviews or self.snapshot(policy, snapshot.pr).identity != snapshot.identity:
            raise GitHubError("fixture review is disabled or stale")
        if any(f.status != "supported" or not self._anchor(snapshot, f) for f in findings):
            raise GitHubError("fixture review lacks a supported anchored finding")
        record = {"id": "fixture-review-" + str(len(self.reviews) + 1),
                  "head_sha": snapshot.head_sha,
                  "findings": [f.model_dump(mode="json") for f in findings],
                  "simulated": True}
        if guard:
            guard()
        self.reviews.append(record)
        return record["id"]

    def checks(self, policy, snapshot):
        self._scope(policy, snapshot)
        current = self.snapshot(policy, snapshot.pr)
        if current.head_sha != snapshot.head_sha:
            raise GitHubError("fixture CI queried with a stale revision")
        result = {"head_sha": snapshot.head_sha,
                  "check_runs": [{"name": "fixture-ci", "head_sha": snapshot.head_sha,
                                  "status": "completed", "conclusion": "success", "app": {"id": 42}}],
                  "statuses": [], "simulated": True}
        self.ci_observations.append(result)
        return result

    def _get(self, policy, path):
        prefix = f"/repos/{policy.repo}/git/commits/"
        if not path.startswith(prefix):
            raise GitHubError("external GitHub API is unavailable in the offline demo")
        sha = path[len(prefix):]
        actual = self._git(self.remote, ["rev-parse", "--verify", sha + "^{commit}"])
        tree = self._git(self.remote, ["rev-parse", actual + "^{tree}"])
        raw = self._git(self.remote, ["cat-file", "commit", actual])
        parents = [line[7:] for line in raw.split("\n\n", 1)[0].splitlines()
                   if line.startswith("parent ")]
        return {"sha": actual, "tree": {"sha": tree}, "parents": [{"sha": p} for p in parents]}


def _expected_state(result: dict | None, state: str) -> dict:
    if not result or result.get("state") != state:
        actual = result.get("state") if result else "no_job"
        raise RuntimeError(f"offline demo expected {state}, observed {actual}")
    return result


def run_demo(output: Path, *, image: str = DEFAULT_IMAGE) -> dict:
    """Run signed intake through exact approval and CAS using only generated fixtures."""
    output = Path(output).absolute()
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("demo output must be a new or empty real directory")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = output / "fixture-source"
    source.mkdir(mode=0o700)
    (source / "src").mkdir()
    _fixture_git(source, ["init", "--quiet", "--template=", "-b", "main"])
    (source / "src/fees.py").write_text(_BASE)
    _fixture_git(source, ["add", "."])
    _fixture_git(source, ["commit", "--quiet", "-m", "owner delivery contract baseline"])
    base = _fixture_git(source, ["rev-parse", "HEAD"])
    _fixture_git(source, ["checkout", "--quiet", "-b", "feature"])
    (source / "src/fees.py").write_text(_HEAD)
    _fixture_git(source, ["add", "."])
    _fixture_git(source, ["commit", "--quiet", "-m", "fixture boundary regression"])
    head = _fixture_git(source, ["rev-parse", "HEAD"])
    repair = _fixture_git(source, ["diff", "--no-ext-diff", "--no-textconv", head, base,
                                   "--", "src/fees.py"]) + "\n"
    remote = output / "fixture-remote.git"
    _fixture_git(output, ["clone", "--quiet", "--bare", "--no-hardlinks", "--", str(source), str(remote)])

    policy = RepoPolicy(tenant="demo", team="payments", installation_id=1, repository_id=2,
                        repo="demo/controlled-fixture", publish_enabled=True, post_reviews=True,
                        approvers=("alice",), checks=(delivery_fee_probe(),),
                        requirements=("Delivery is free at subtotal 50 or above; otherwise costs 5.",),
                        required_github_checks=("fixture-ci",), required_github_check_app_ids=(42,))
    (output / "fixture-policy.json").write_text(policy.model_dump_json(indent=2))

    def defective(payload):
        return any("subtotal > 50" in item["content"] for item in payload.get("context", ()))

    def review(payload):
        return {"findings": [_FINDING] if defective(payload) else [],
                "limitations": ["Offline fixture response; no real model evaluated this code."]}

    def refute(payload):
        findings = []
        for proposal in payload.get("proposals", ()):
            findings.append({**proposal, "status": "supported" if defective(payload) else "refuted"})
        return {"findings": findings}

    def repair_fixture(payload):
        if not defective(payload):
            raise ValueError("fixture repair must be grounded in captured defective source")
        return {"patch": repair, "rationale": "Restore the owner-specified fifty boundary."}

    model = FixtureModel({"review": review, "refute": refute, "repair": repair_fixture})
    artifacts = ArtifactStore(output / "artifacts")
    store = Store("sqlite:///" + str(output / "state.db"))
    store.initialize()
    forge = LocalForge(remote)
    webhook_secret, approval_secret = secrets.token_bytes(32), secrets.token_bytes(32)
    bearer = secrets.token_urlsafe(32)
    principal = Principal(actor="alice", tenant=policy.tenant, teams=(policy.team,),
                          roles=("read", "approve", "operate"),
                          token_digest=hashlib.sha256(bearer.encode()).hexdigest())
    app = create_app(store, [policy], [principal], webhook_secret=webhook_secret,
                     approval_secret=approval_secret)
    def request(method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://offline-demo",
                                         trust_env=False) as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    pipeline = Pipeline(store, [policy], forge, model, DockerVerifier(image, artifacts=artifacts),
                        artifacts, publisher=Publisher(forge, artifacts, approval_secret))
    try:
        body = json.dumps({"action": "opened", "number": 17,
                           "installation": {"id": 1},
                           "repository": {"id": 2, "full_name": policy.repo}},
                          sort_keys=True, separators=(",", ":")).encode()
        headers = {"x-hub-signature-256": "sha256=" + hmac.new(
                   webhook_secret, body, hashlib.sha256).hexdigest(),
                   "x-github-event": "pull_request", "x-github-delivery": "offline-demo-delivery"}
        received = request("POST", "/webhooks/github", content=body, headers=headers)
        duplicate = request("POST", "/webhooks/github", content=body, headers=headers)
        if received.status_code != 202 or duplicate.json() != received.json():
            raise RuntimeError("signed fixture webhook was not durably deduplicated")
        reviewed = _expected_state(pipeline.run_once("demo-reviewer"), "awaiting_approval")
        reported = _expected_state(pipeline.run_once("demo-reporter", queue="publish"), "review_reported")
        candidate_id = reviewed["candidate_id"]
        record = store.get_record(policy.tenant, "candidate", candidate_id)
        candidate = Candidate.model_validate_json(json.dumps(record["candidate"]))
        approved = request("POST", f"/v1/candidates/{candidate_id}/approve",
                               headers={"authorization": "Bearer " + bearer})
        if approved.status_code != 202:
            raise RuntimeError("authenticated exact-candidate approval failed")
        published = _expected_state(pipeline.run_once("demo-publisher", queue="publish"), "published")
        observed = _expected_state(pipeline.run_once("demo-observer", queue="observe"),
                                   "ci_observed_rereview_queued")
        rereviewed = _expected_state(pipeline.run_once("demo-rereviewer"), "review_complete")
        actual_head = _fixture_git(remote, ["rev-parse", "refs/heads/feature"])
        actual_base = _fixture_git(remote, ["rev-parse", "refs/heads/main"])
        actual_tree = _fixture_git(remote, ["rev-parse", actual_head + "^{tree}"])
        actual_parent = _fixture_git(remote, ["rev-parse", actual_head + "^1"])
        if (actual_head != candidate.commit_sha or actual_tree != candidate.tree_sha
                or actual_parent != head or actual_base != base):
            raise RuntimeError("actual local bare-Git readback did not match the approved candidate")
        audit = request("GET", "/v1/audit", headers={"authorization": "Bearer " + bearer})
        if audit.status_code != 200:
            raise RuntimeError("authenticated demo audit export failed")
        receipt = {"schema": "patchington-offline-demo-v1", "status": "completed",
                   "simulated": ["model generation", "GitHub PR/review metadata", "GitHub CI result"],
                   "executed": ["signed ASGI webhook", "durable SQLite development queue",
                                "independent Docker regression probes", "authenticated ASGI approval",
                                "clean publisher reconstruction", "local bare-Git compare-and-swap",
                                "Git object/ref readback", "durable CI observation and rereview queues"],
                   "webhook": {"status_code": received.status_code, **received.json(),
                               "deduplicated": duplicate.json() == received.json()},
                   "review": reviewed, "review_report": reported,
                   "approval": {"status_code": approved.status_code, **approved.json(), "actor": "alice"},
                   "candidate": record["candidate"], "verification": record["verification"],
                   "publication": published, "observation": observed, "rereview": rereviewed,
                   "remote_readback": {"expected_before": head, "actual_head": actual_head,
                                       "actual_tree": actual_tree, "actual_parent": actual_parent,
                                       "base_unchanged": actual_base == base},
                   "model_calls": model.calls, "reviews": forge.reviews,
                   "ci_observations": forge.ci_observations,
                   "audit_digest": artifacts.put_json({"events": audit.json()}),
                   "jobs": [{"id": job.id, "queue_kind": job.payload["kind"], "status": job.status,
                             "result_state": (job.result or {}).get("state")}
                            for job in store.list_jobs(policy.tenant)],
                   "limits": ["No real model quality, GitHub authentication, live CI or production scale claim.",
                              "Publish enablement and simulated approver apply only to the generated local fixture.",
                              "No merge or release operation is performed."]}
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return receipt
    finally:
        forge._http.close()
        store.close()
