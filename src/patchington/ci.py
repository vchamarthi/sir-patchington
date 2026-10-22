"""Bounded read-only GitHub CI observations and safe review-summary rendering.

Endpoints and permissions verified against GitHub's REST documentation:
https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2026-03-10
https://docs.github.com/en/rest/actions/workflow-jobs?apiVersion=2026-03-10
https://docs.github.com/en/rest/checks/runs?apiVersion=2026-03-10
https://docs.github.com/en/rest/commits/statuses?apiVersion=2026-03-10
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import UTC, datetime

import httpx

from .context import _signature

MAX_RUNS = 20
MAX_JOBS = 100
MAX_RECORDS = 100
MAX_REQUESTS = 40
MAX_LOG_BYTES = 64_000
MAX_RESPONSE_BYTES = 500_000
_FAILURES = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
_PENDING = {"queued", "in_progress", "pending", "requested", "waiting"}
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_ERROR_LINE = re.compile(
    r"error|fail|traceback|exception|assert|not found|timed?\s*out|exit code|permission denied|"
    r"^.*:\d+:\d+:\s+[A-Z]\d{3}\b",
    re.IGNORECASE)


def _text(value, limit=500):
    if not isinstance(value, str):
        return ""
    # Inspect the entire field before truncation; never retain a private-key body.
    if _signature(value):
        return "[withheld: credential signature]"
    value = _ANSI.sub("", value)
    value = "".join(char for char in value if char in "\n\t" or ord(char) >= 32)
    if _signature(value):
        return "[withheld: credential signature]"
    return value.encode()[:limit].decode("utf-8", errors="ignore")


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _state(status, conclusion=None):
    if status in {"failure", "error"} or conclusion in _FAILURES:
        return "failure"
    if status in _PENDING:
        return "pending"
    if status == "success" or status == "completed" and conclusion == "success":
        return "success"
    return "unknown"


def _diagnosis(status, conclusion, steps, excerpt):
    failing = [step["name"] for step in steps if step["conclusion"] in _FAILURES]
    state = _state(status, conclusion)
    if state == "failure":
        reason = ("Failed step(s): " + ", ".join(failing) if failing else
                  "GitHub reports conclusion: " + str(conclusion or status))
        label = "observed failure"
        actions = ["Inspect the failed step and its reported error; reproduce the same check locally."]
        cause = "Unknown from the observed evidence"
        if "ModuleNotFoundError:" in excerpt:
            label = "likely cause"
            cause = "Missing Python dependency or import path"
            actions = ["Check dependency installation and Python import paths in the failing job."]
        elif re.search(r"AssertionError|(?m:^FAILED .*::)", excerpt):
            cause = "A test reported an assertion failure; the underlying code cause is unknown"
            actions = ["Reproduce the named failing test and compare its expected behavior to the code; "
                       "do not change test expectations without evidence."]
        elif re.search(r"SyntaxError|IndentationError|TabError", excerpt):
            cause = "The language parser reported invalid syntax"
            actions = ["Inspect the reported file and line, correct the syntax, then rerun the same parser."]
        elif re.search(r"error TS\d+|\b(?:reportArgumentType|reportAssignmentType)\b|"
                       r"\berror:.*\[(?:arg-type|assignment|return-value|union-attr)\]", excerpt):
            cause = "The type checker reported a type incompatibility"
            actions = ["Inspect the reported types and caller contract, then rerun the same type checker."]
        elif re.search(r"(?m)^.*:\d+:\d+:\s+(?:[A-Z]\d{3}|error\s)", excerpt):
            cause = "A static-analysis rule reported a violation"
            actions = ["Inspect the reported rule and source location, apply a behavior-preserving fix, "
                       "then rerun the same linter."]
        elif conclusion == "timed_out":
            cause = "GitHub reports the configured job timeout was exceeded; the blocking cause is unknown"
            actions = ["Inspect the last active step and its runtime or blocking operation before "
                       "changing timeouts or rerunning."]
        elif conclusion == "cancelled":
            cause = "The job was cancelled; the initiator or concurrency condition is unknown"
            actions = ["Inspect cancellation events and workflow concurrency before deciding to rerun."]
        elif re.search(r"No space left on device|runner has received a shutdown signal|"
                       r"lost communication with the server", excerpt, re.IGNORECASE):
            label, cause = "likely cause", "Runner storage or availability failure"
            actions = ["Inspect runner capacity and availability using the reported error; "
                       "retry only after the infrastructure condition is understood."]
        return reason, {"label": label, "evidence": _text(excerpt, 1_000) or reason,
                        "possible_cause": cause,
                        "next_actions": actions}
    if state == "pending":
        reason = "GitHub reports the job is still pending or running"
        actions = ["Wait for completion, then collect CI again for this commit."]
    elif state == "success":
        reason, actions = "GitHub reports success", ["No failure action indicated by this observation."]
    else:
        reason = "No executed success or concrete failure is established"
        actions = ["Inspect the workflow configuration and missing or skipped job evidence."]
    return reason, {"label": "unknown", "evidence": reason,
                    "possible_cause": "Unknown from the observed evidence", "next_actions": actions}


class _Collector:
    def __init__(self, client, policy, snapshot):
        self.client, self.policy, self.snapshot = client, policy, snapshot
        self.deadline, self.requests = time.monotonic() + 45, 0
        self.root = f"/repos/{policy.repo}"
        self.report = {"head_sha": snapshot.head_sha, "repository_id": policy.repository_id,
            "repo": policy.repo, "observed_at": datetime.now(UTC).isoformat(),
            "source": "github_rest", "state": "unknown", "complete": True,
            "workflows": [], "checks": [], "statuses": [], "superseded_runs": [], "limitations": [
                "CI observations are for this exact head commit; required-check coverage is not proven.",
                "Only static CI results are inspected; diagnosis is evidence-based and may remain unknown.",
                "Logs are bounded error excerpts with credential-pattern filtering, not complete DLP."]}

    def partial(self, message):
        self.report["complete"] = False
        if message not in self.report["limitations"]:
            self.report["limitations"].append(message)

    def request(self, path, *, raw=False):
        from .github import GitHubError
        if self.requests >= MAX_REQUESTS or time.monotonic() >= self.deadline:
            raise GitHubError("CI request/time budget exhausted")
        self.requests += 1
        return self.client._request(self.policy, "GET", path, ci=True,
            response_limit=MAX_LOG_BYTES if raw else MAX_RESPONSE_BYTES, expect_json=not raw,
            timeout=max(0.1, min(5, self.deadline - time.monotonic())), deadline=self.deadline)

    def pages(self, path, key=None, limit=MAX_RECORDS):
        from .github import API_URL, GitHubError
        initial = httpx.URL(API_URL + path)
        allowed_paths = {initial.path, initial.path.replace(
            self.root + "/", f"/repositories/{self.policy.repository_id}/", 1)}
        fixed = {k: v for k, v in initial.params.items() if k not in {"page", "per_page"}}
        current, seen, result, total = str(initial), set(), [], None
        for _ in range(4):
            try:
                url = httpx.URL(current)
                if (url.scheme != "https" or url.host != "api.github.com"
                        or url.port not in (None, 443) or url.userinfo or url.fragment
                        or url.path not in allowed_paths or current in seen
                        or {k: v for k, v in url.params.items()
                            if k not in {"page", "per_page"}} != fixed
                        or len(url.params.multi_items()) != len(url.params)
                        or not url.params.get("page", "1").isdigit()
                        or not url.params.get("per_page", "100").isdigit()
                        or not 1 <= int(url.params.get("per_page", "100")) <= 100):
                    raise GitHubError("Unsafe or cyclic CI pagination")
                seen.add(current)
                response = self.request(url.raw_path.decode())
                body = response.json()
                rows = body.get(key) if key and isinstance(body, dict) else body
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise GitHubError("Malformed CI listing")
                if key:
                    count = body.get("total_count")
                    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                        raise GitHubError("Missing CI listing coverage")
                    total = max(total or 0, count)
                remaining = max(0, limit - len(result))
                result.extend(rows[:remaining])
                next_link = response.links.get("next", {}).get("url")
                if len(rows) > remaining:
                    self.partial("CI listing exceeds its record bound.")
                if not next_link:
                    if total is not None and total > len(result):
                        self.partial("CI listing was incomplete or exceeded its record bound.")
                    return result
                if len(result) >= limit:
                    self.partial("CI pagination exceeds its record bound.")
                    return result
                current = next_link
            except (GitHubError, ValueError, TypeError, KeyError):
                self.partial("CI endpoint unavailable, malformed, or pagination rejected: " + initial.path)
                return result
        self.partial("CI pagination exceeds its page bound.")
        return result

    def log(self, job_id):
        from .github import GitHubError
        try:
            scope, truncated = "full_bounded_job_log", False
            response = self.request(f"{self.root}/actions/jobs/{job_id}/logs", raw=True)
            if response.status_code == 302:
                location = httpx.URL(response.headers.get("Location", ""))
                host = location.host or ""
                if (location.scheme != "https" or location.userinfo or location.fragment
                        or location.port not in (None, 443)
                        or not any(host.endswith(suffix) and host != suffix[1:] for suffix in
                                   (".actions.githubusercontent.com", ".blob.core.windows.net"))):
                    raise GitHubError("Unqualified CI log download host")
                if self.requests >= MAX_REQUESTS or time.monotonic() >= self.deadline:
                    raise GitHubError("CI log budget exhausted")
                self.requests += 1
                # A fresh client has no App headers, cookies, proxy settings or redirects.
                with httpx.Client(transport=self.client._transport, trust_env=False,
                                  follow_redirects=False,
                                  timeout=max(0.1, min(5, self.deadline - time.monotonic()))) as download:
                    with download.stream("GET", location, headers={
                            "Range": f"bytes=-{MAX_LOG_BYTES}", "Accept-Encoding": "identity"}) as raw:
                        if raw.status_code not in {200, 206}:
                            raise GitHubError("CI log download unavailable")
                        expected_bytes = None
                        if raw.status_code == 206:
                            matched = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                                   raw.headers.get("Content-Range", ""))
                            if matched is None:
                                raise GitHubError("Malformed CI log tail range")
                            start, end, total = map(int, matched.groups())
                            expected_bytes = end - start + 1
                            if (not 0 <= start <= end < total or end != total - 1
                                    or expected_bytes > MAX_LOG_BYTES):
                                raise GitHubError("CI log response was not a bounded tail")
                            scope, truncated = "job_log_tail", start > 0
                        data = bytearray()
                        for chunk in raw.iter_bytes():
                            if len(data) + len(chunk) > MAX_LOG_BYTES or time.monotonic() > self.deadline:
                                raise GitHubError("CI log byte/time bound exceeded")
                            data.extend(chunk)
                        if expected_bytes is not None and len(data) != expected_bytes:
                            raise GitHubError("Incomplete CI log tail response")
                        text = bytes(data).decode("utf-8", errors="replace")
            else:
                text = response.text
            if _signature(text):
                return {"state": "withheld", "excerpt": "[withheld: credential signature]",
                        "scope": scope, "truncated": truncated}
            lines = [line for line in _text(text, MAX_LOG_BYTES).splitlines() if _ERROR_LINE.search(line)]
            excerpt = "\n".join(lines[:20])
            truncated = truncated or len(lines) > 20 or len(excerpt.encode()) > 3_000
            if scope == "job_log_tail":
                note = "Job log evidence uses bounded tail excerpts; earlier log context may be omitted."
                if note not in self.report["limitations"]:
                    self.report["limitations"].append(note)
            return {"state": "available", "excerpt": _text(excerpt, 3_000),
                    "scope": scope, "truncated": truncated}
        except (GitHubError, httpx.HTTPError, ValueError, TypeError):
            self.partial("Some job logs are unavailable, expired, unqualified, or exceed bounds.")
            return {"state": "unavailable", "excerpt": ""}

    def collect(self):
        from .github import GitHubError
        sha = self.snapshot.head_sha
        try:
            repository = self.request(self.root).json()
            if (repository.get("id") != self.policy.repository_id
                    or repository.get("full_name", "").lower() != self.policy.repo.lower()):
                raise GitHubError("CI repository identity mismatch")
        except (GitHubError, ValueError, AttributeError):
            self.partial("CI repository identity could not be verified.")
            self.report["state"] = "unavailable"
            return self.report
        checks = self.pages(f"{self.root}/commits/{sha}/check-runs?per_page=100&filter=latest",
                            "check_runs")
        seen = set()
        for check in checks:
            identity = check.get("id")
            if not _positive(identity) or identity in seen or check.get("head_sha") != sha:
                self.partial("Check identity or head commit mismatch; record withheld.")
                continue
            seen.add(identity)
            output = check.get("output") or {}
            if not isinstance(output, dict):
                self.partial("Malformed check output; evidence unavailable.")
                output = {}
            evidence = _text("\n".join(str(output.get(key) or "")
                                      for key in ("title", "summary", "text")), 2_000).strip()
            row = {"id": identity, "head_sha": sha,
                "name": _text(check.get("name"), 150), "status": _text(check.get("status"), 40),
                "conclusion": _text(check.get("conclusion"), 40), "evidence": evidence,
                "annotations": []}
            if not evidence and _state(row["status"], row["conclusion"]) == "failure":
                annotations = self.pages(f"{self.root}/check-runs/{identity}/annotations?per_page=100",
                                         limit=20)
                row["annotations"] = [{"path": _text(annotation.get("path"), 150),
                    "message": _text(annotation.get("message"), 1_000),
                    "level": _text(annotation.get("annotation_level"), 40)}
                    for annotation in annotations]
                row["evidence"] = _text("\n".join(
                    annotation["message"] for annotation in row["annotations"]), 2_000)
            self.report["checks"].append(row)
        statuses = self.pages(f"{self.root}/commits/{sha}/statuses?per_page=100")
        contexts = set()
        for status in statuses:
            name = status.get("context")
            if (not _positive(status.get("id")) or not isinstance(name, str)
                    or status.get("sha", sha) != sha):
                self.partial("Status identity or head commit mismatch; record withheld.")
                continue
            if name in contexts:
                continue  # GitHub returns statuses newest first, per context.
            contexts.add(name)
            self.report["statuses"].append({"id": status["id"], "context": _text(name, 150),
                "state": _text(status.get("state"), 40),
                "description": _text(status.get("description"), 500)})
        runs = self.pages(f"{self.root}/actions/runs?head_sha={sha}&per_page=100", "workflow_runs",
                          MAX_RUNS)
        run_ids, valid_runs, grouped = set(), [], {}
        for run in runs:
            repo, run_id = run.get("repository") or {}, run.get("id")
            if (not _positive(run_id) or run_id in run_ids or run.get("head_sha") != sha
                    or not isinstance(repo, dict) or repo.get("id") != self.policy.repository_id
                    or not isinstance(repo.get("full_name"), str)
                    or repo["full_name"].lower() != self.policy.repo.lower()):
                self.partial("Workflow identity, repository or head commit mismatch; record withheld.")
                continue
            run_ids.add(run_id)
            valid_runs.append(run)
            if (_positive(run.get("workflow_id")) and isinstance(run.get("event"), str)
                    and run["event"] and isinstance(run.get("head_branch"), str)):
                group = run["workflow_id"], run["event"], run["head_branch"]
                previous = grouped.get(group)
                def ordering(item):
                    return (item.get("run_number") if _positive(item.get("run_number"))
                            else 0, item["id"])
                if previous is None or ordering(run) > ordering(previous):
                    grouped[group] = run
        effective_ids = {run["id"] for run in grouped.values()}
        job_ids = set()
        for run in valid_runs:
            run_id = run["id"]
            has_group = (_positive(run.get("workflow_id")) and isinstance(run.get("event"), str)
                         and run["event"] and isinstance(run.get("head_branch"), str))
            if has_group and run_id not in effective_ids:
                self.report["superseded_runs"].append(run_id)
                continue
            row = {"id": run_id, "name": _text(run.get("name"), 150), "head_sha": sha,
                   "status": _text(run.get("status"), 40),
                   "conclusion": _text(run.get("conclusion"), 40), "jobs": []}
            self.report["workflows"].append(row)
            jobs = self.pages(f"{self.root}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
                              "jobs", MAX_JOBS - len(job_ids))
            for job in jobs:
                job_id = job.get("id")
                if (not _positive(job_id) or job_id in job_ids or job.get("run_id") != run_id
                        or job.get("head_sha") != sha):
                    self.partial("Job identity, workflow or head commit mismatch; record withheld.")
                    continue
                job_ids.add(job_id)
                steps = job.get("steps") or []
                if not isinstance(steps, list):
                    self.partial("Malformed CI job steps.")
                    steps = []
                if len(steps) > 50:
                    self.partial("CI step listing exceeds its record bound.")
                if any(not isinstance(step, dict) or not _positive(step.get("number"))
                       for step in steps):
                    self.partial("Malformed CI job step identity; record withheld.")
                steps = [{"number": step["number"], "name": _text(step.get("name"), 150),
                          "status": _text(step.get("status"), 40),
                          "conclusion": _text(step.get("conclusion"), 40)}
                         for step in steps[:50]
                         if isinstance(step, dict) and _positive(step.get("number"))]
                status, conclusion = _text(job.get("status"), 40), _text(job.get("conclusion"), 40)
                log = self.log(job_id) if _state(status, conclusion) == "failure" else {
                    "state": "not_requested", "excerpt": ""}
                reason, diagnosis = _diagnosis(status, conclusion, steps, log["excerpt"])
                row["jobs"].append({"id": job_id, "run_id": run_id, "head_sha": sha,
                    "name": _text(job.get("name"), 150), "status": status, "conclusion": conclusion,
                    "steps": steps, "failure_reason": reason, "diagnosis": diagnosis, "log": log})
            if not row["jobs"] and _state(row["status"], row["conclusion"]) == "success":
                self.partial("A successful workflow had no observed jobs; execution coverage is unknown.")
        if self.report["superseded_runs"]:
            self.report["limitations"].append(
                "Older runs superseded by a newer run for the same workflow/event/branch were omitted; "
                "only effective latest runs contribute to CI state within this bounded observation.")
        states = [_state(item["status"], item["conclusion"]) for item in self.report["checks"]]
        states += [_state(item["state"]) for item in self.report["statuses"]]
        for run in self.report["workflows"]:
            states.append(_state(run["status"], run["conclusion"]))
            states += [_state(job["status"], job["conclusion"]) for job in run["jobs"]]
        if "failure" in states:
            self.report["state"] = "failure"
        elif not self.report["complete"]:
            self.report["state"] = "unknown" if states else "unavailable"
        elif "pending" in states:
            self.report["state"] = "pending"
        elif states and all(state == "success" for state in states):
            self.report["state"] = "success"
        return self.report


def collect_ci(client, policy, snapshot):
    return _Collector(client, policy, snapshot).collect()


def model_ci_context(report: dict, budget: int = 20_000) -> dict:
    """Prioritize failures in a bounded model view while preserving captured evidence separately."""
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 512:
        raise ValueError("CI model budget must be an integer of at least 512 bytes")
    result = {"head_sha": _text(report.get("head_sha"), 40), "state": _text(report.get("state"), 30),
              "complete": report.get("complete") is True,
              "source": _text(report.get("source"), 50), "jobs": [], "checks": [], "statuses": [],
              "omitted_records": 0, "clipped_fields": 0, "truncated": False}

    def compact(value, limit):
        if isinstance(value, str) and len(value.encode()) > limit:
            result["clipped_fields"] += 1
        return _text(value, limit)

    candidates = []
    for workflow in report.get("workflows", [])[:MAX_RUNS]:
        for job in workflow.get("jobs", [])[:MAX_JOBS]:
            record = {"id": job.get("id"), "workflow": _text(workflow.get("name"), 100),
                      "name": _text(job.get("name"), 100), "status": _text(job.get("status"), 40),
                      "conclusion": _text(job.get("conclusion"), 40),
                      "failure_reason": compact(job.get("failure_reason"), 300),
                      "evidence": compact((job.get("log") or {}).get("excerpt"), 400),
                      "evidence_scope": _text((job.get("log") or {}).get("scope"), 40),
                      "evidence_truncated": (job.get("log") or {}).get("truncated") is True,
                      "next_actions": [_text(action, 300) for action in
                                       (job.get("diagnosis") or {}).get("next_actions", [])[:2]]}
            candidates.append((_state(record["status"], record["conclusion"]), "jobs", record))
    for check in report.get("checks", [])[:MAX_RECORDS]:
        record = {"id": check.get("id"), "name": _text(check.get("name"), 100),
                  "status": _text(check.get("status"), 40),
                  "conclusion": _text(check.get("conclusion"), 40),
                  "evidence": compact(check.get("evidence"), 400)}
        candidates.append((_state(record["status"], record["conclusion"]), "checks", record))
    for status in report.get("statuses", [])[:MAX_RECORDS]:
        record = {"id": status.get("id"), "context": _text(status.get("context"), 100),
                  "state": _text(status.get("state"), 40),
                  "description": _text(status.get("description"), 300)}
        candidates.append((_state(record["state"]), "statuses", record))
    priority = {"failure": 0, "pending": 1, "unknown": 2, "success": 3}
    for _, kind, record in sorted(candidates, key=lambda row: priority[row[0]]):
        result[kind].append(record)
        # Reserve the final omission counter/truncation fields' worst-case growth.
        if len(json.dumps(result).encode()) + 30 > budget:
            result[kind].pop()
            result["omitted_records"] += 1
    result["truncated"] = result["omitted_records"] > 0 or result["clipped_fields"] > 0
    return result


def render_ci_summary(report: dict) -> str:
    """HTML-escape all untrusted CI fields; bound output for GitHub comment publication."""
    def text(value, limit=1_000):
        return html.escape(_text(str(value or ""), limit), quote=True).replace("@", "&#64;")

    rows = ["<details><summary>CI status and failures</summary>\n",
            "<p>Commit: <code>" + text(report.get("head_sha"), 40) + "</code><br>",
            "Observed: <code>" + text(report.get("observed_at")) + "</code> via "
            + text(report.get("source")) + "<br>",
            "CI state: <strong>" + text(report.get("state")) + "</strong>; coverage: "
            + ("bounded observation complete" if report.get("complete") else "partial/unavailable")
            + ".</p>"]
    for workflow in report.get("workflows", [])[:MAX_RUNS]:
        rows.append("<p>Workflow <code>" + text(workflow.get("name"), 150) + "</code>: "
                    + text(workflow.get("status")) + " / " + text(workflow.get("conclusion")) + "</p>")
        for job in workflow.get("jobs", [])[:MAX_JOBS]:
            diagnosis = job.get("diagnosis") or {}
            row = ("<p>Job <code>" + text(job.get("name"), 150) + "</code>: "
                + text(job.get("status")) + " / " + text(job.get("conclusion")) + "<br>"
                + "Reason: " + text(job.get("failure_reason")) + "<br>Diagnosis ("
                + text(diagnosis.get("label")) + "): " + text(diagnosis.get("possible_cause"))
                + "<br>Next action: " + text(" ".join(diagnosis.get("next_actions", []))) + "</p>")
            excerpt = (job.get("log") or {}).get("excerpt")
            if excerpt:
                row += "<pre>" + text(excerpt, 3_000) + "</pre>"
            elif (job.get("log") or {}).get("state") == "unavailable":
                row += "<p>Job logs unavailable; diagnosis is limited to observed status and steps.</p>"
            if (job.get("log") or {}).get("scope") == "job_log_tail":
                row += "<p>Log evidence is a bounded tail; earlier log context may be omitted.</p>"
            rows.append(row)
    for check in report.get("checks", [])[:MAX_RECORDS]:
        rows.append("<p>Check <code>" + text(check.get("name"), 150) + "</code>: "
                    + text(check.get("status")) + " / " + text(check.get("conclusion"))
                    + "<br>Observed evidence: " + text(check.get("evidence"), 1_000) + "</p>")
    for status in report.get("statuses", [])[:MAX_RECORDS]:
        rows.append("<p>Status <code>" + text(status.get("context"), 150) + "</code>: "
                    + text(status.get("state")) + " — " + text(status.get("description")) + "</p>")
    rows += ["<p>Limitation: " + text(limit) + "</p>" for limit in report.get("limitations", [])[:30]]
    bounded, used = [], 0
    for row in rows:
        if used + len(row.encode()) > 40_000:
            bounded.append("<p>Additional CI detail omitted by the publication byte bound.</p>")
            break
        bounded.append(row)
        used += len(row.encode())
    return "\n".join(bounded) + "\n</details>"
