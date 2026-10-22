"""Deterministic review admission; source-grounded judgments do not certify repairs."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .contracts import ChangeSnapshot, Finding, ReviewVerification
from .review_context import line_selected as _line_selected

REVIEW_PIPELINE_VERSION = "v5-function-hunk-context"


def added_lines(patch: str) -> set[int]:
    """Only added RIGHT-side lines from complete hunks; malformed patches abstain."""
    result: set[int] = set()
    old_left = new_left = 0
    line = None
    for row in patch.splitlines():
        match = re.match(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", row)
        if match:
            if old_left or new_left:
                return set()
            old_left = int(match[1]) if match[1] is not None else 1
            new_left = int(match[3]) if match[3] is not None else 1
            line = int(match[2])
        elif line is not None and row.startswith("\\ No newline"):
            continue
        elif line is not None and (old_left or new_left):
            if row.startswith("+"):
                result.add(line)
                new_left -= 1
                line += 1
            elif row.startswith("-"):
                old_left -= 1
            elif row.startswith(" "):
                old_left -= 1
                new_left -= 1
                line += 1
            else:
                return set()
            if old_left < 0 or new_left < 0:
                return set()
    return set() if old_left or new_left else result


def _normalized(value: str) -> str:
    # Case and interior whitespace can be significant in quoted source literals.
    return value.strip()


def select_proposals(findings: list[Finding], snapshot: ChangeSnapshot,
                     context: list[dict]) -> tuple[dict[str, Finding], list[dict]]:
    """Collapse exact cross-lane repeats; preserve distinct locations and outcomes."""
    sources = {item["path"]: item for item in context}
    changed = {item.path: added_lines(item.patch) for item in snapshot.files}
    decisions, selected = [], {}
    severity = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for finding in sorted(findings, key=lambda f: (severity[f.severity], f.fingerprint)):
        reason = None
        if finding.path not in changed:
            reason = "outside_changed_files"
        elif finding.line not in changed[finding.path]:
            reason = "outside_changed_lines"
        elif finding.path not in sources:
            reason = "source_not_selected"
        elif finding.line > len(sources[finding.path]["content"].splitlines()):
            reason = "invalid_source_anchor"
        elif not _line_selected(sources[finding.path], finding.line):
            reason = "source_line_not_selected"
        key = (finding.path, finding.line, _normalized(finding.claim),
               _normalized(finding.trigger), _normalized(finding.expected))
        if not reason and key in selected:
            reason = "duplicate"
        if reason:
            decisions.append({"fingerprint": finding.fingerprint, "reason": reason,
                              **({"duplicate_of": selected[key].fingerprint}
                                 if reason == "duplicate" else {})})
        else:
            selected[key] = finding.model_copy(update={"status": "proposed"})
    return {finding.fingerprint: finding for finding in selected.values()}, decisions


def verify_judgments(proposals: dict[str, Finding], verification: ReviewVerification,
                     context: list[dict], snapshot: ChangeSnapshot) -> tuple[tuple[Finding, ...], list[dict]]:
    """Check exact bytes/ranges and bind verdicts to immutable original proposals.

    A matching citation proves source grounding, not logical truth, execution or
    model independence. Conflicting, missing and unverifiable verdicts abstain.
    """
    sources = {item["path"]: item for item in context
               if item["revision"] == snapshot.head_sha
               and item["repository_id"] == snapshot.repository_id
               and item["repo"] == snapshot.repo and item["tenant"] == snapshot.tenant}
    grouped = defaultdict(list)
    for judgment in verification.judgments:
        grouped[judgment.fingerprint].append(judgment)
    findings, decisions = [], []
    for fingerprint, proposal in proposals.items():
        judgments = grouped.pop(fingerprint, [])
        status, reason = "needs_evidence", "missing_judgment"
        if len(judgments) > 1:
            reason = "conflicting_or_duplicate_judgments"
        elif len(judgments) == 1:
            judgment = judgments[0]
            status, reason = judgment.status, "model_judgment"
            if status in {"supported", "refuted"}:
                valid = bool(judgment.citations)
                anchored = False
                for citation in judgment.citations:
                    source = sources.get(citation.path)
                    if source is None:
                        valid = False
                        continue
                    content = source["content"]
                    lines = content.splitlines()
                    if (citation.source_digest != source["digest"]
                            or hashlib.sha256(content.encode()).hexdigest() != source["digest"]
                            or citation.end_line > len(lines)
                            or "\n".join(lines[citation.start_line - 1:citation.end_line])
                            != citation.quote):
                        valid = False
                    if not _line_selected(source, citation.start_line, citation.end_line):
                        valid = False
                    if (citation.path == proposal.path
                            and citation.start_line <= proposal.line <= citation.end_line):
                        anchored = True
                if not valid or not anchored:
                    status, reason = "needs_evidence", "unverifiable_source_citations"
        findings.append(proposal.model_copy(update={"status": status}))
        decisions.append({"fingerprint": fingerprint, "status": status, "reason": reason,
                          "judgments": [item.model_dump(mode="json") for item in judgments]})
    for fingerprint in sorted(grouped):
        decisions.append({"fingerprint": fingerprint, "status": "ignored",
                          "reason": "unknown_proposal"})
    return tuple(findings), decisions
