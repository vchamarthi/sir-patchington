"""Review-quality control evidence from fixtures, not a model accuracy benchmark."""

import copy
import json

import pytest
from test_pipeline import FINDING, fixture, supported_judgments

from patchington.context import ContextIndex
from patchington.contracts import Finding, ReviewVerification, digest_json
from patchington.execution import LocalVerifier
from patchington.pipeline import Pipeline
from patchington.review import added_lines, select_proposals, verify_judgments


def run_review(tmp_path, *, finding=None):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    def ungrounded(payload):
        result = supported_judgments(payload)
        for judgment in result["judgments"]:
            judgment["citations"] = []
        return result
    model.responses["refute"] = ungrounded
    if finding is not None:
        model.responses["review"] = {"findings": [finding]}
    job = store.enqueue("acme", policy.repo, 17, "quality", {})
    result = Pipeline(store, [policy], forge, model,
                      LocalVerifier(trusted=True, artifacts=artifacts), artifacts).run_once("worker")
    return result, store.get_record("acme", "findings", job), model


def test_a_critic_support_label_without_source_evidence_cannot_start_repair(tmp_path):
    result, record, model = run_review(tmp_path)
    assert result["state"] == "needs_owner"
    assert record["findings"][0]["status"] == "needs_evidence"
    assert "repair" not in model.calls


def test_unchanged_context_line_cannot_be_promoted_to_a_changed_line_finding(tmp_path):
    result, record, model = run_review(tmp_path, finding={**FINDING, "line": 1})
    assert "repair" not in model.calls
    assert not any(f["status"] == "supported" for f in record["findings"])
    assert any(item["reason"] == "outside_changed_lines" for item in record["filtering"])
    assert not result["verification_passed"]


def test_findings_at_distinct_locations_or_expected_outcomes_keep_distinct_identity():
    original = Finding.model_validate(FINDING)
    assert original.fingerprint != original.model_copy(update={"line": 3}).fingerprint
    assert original.fingerprint != original.model_copy(update={"expected": "5"}).fingerprint


def evidence(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    snapshot = forge.snapshot(policy, 17)
    index = ContextIndex()
    index.add(snapshot, {"src/fees.py": "def delivery_fee(subtotal):\n"
                       "    return 0 if subtotal > 50 else 5\n"}, policy)
    context = index.retrieve(snapshot, policy)
    proposal = Finding.model_validate(FINDING)
    payload = {"context": context, "proposals": [
        {**FINDING, "fingerprint": proposal.fingerprint}]}
    return snapshot, context, proposal, supported_judgments(payload)


@pytest.mark.parametrize("status", ["supported", "refuted"])
@pytest.mark.parametrize("tamper", ["no_citations", "wrong_digest", "invented_quote",
                                  "wrong_path", "out_of_range", "wrong_revision"])
def test_invalid_citations_never_support_or_silently_refute_a_claim(tmp_path, status, tamper):
    snapshot, context, proposal, response = evidence(tmp_path)
    judgment = response["judgments"][0]
    judgment["status"] = status
    citation = judgment["citations"][0]
    if tamper == "no_citations":
        judgment["citations"] = []
    elif tamper == "wrong_digest":
        citation["source_digest"] = "0" * 64
    elif tamper == "invented_quote":
        citation["quote"] = "a guard that is not in this revision"
    elif tamper == "wrong_path":
        citation["path"] = "src/unknown.py"
    elif tamper == "out_of_range":
        citation["start_line"] = citation["end_line"] = 50
    else:
        context[0]["revision"] = "a" * 40
    verification = ReviewVerification.model_validate_json(json.dumps(response))
    findings, ledger = verify_judgments({proposal.fingerprint: proposal}, verification,
                                      context, snapshot)
    assert findings[0].status == "needs_evidence"
    assert ledger[0]["reason"] == "unverifiable_source_citations"


def test_verifier_keeps_original_proposal_and_records_counterargument(tmp_path):
    snapshot, context, proposal, response = evidence(tmp_path)
    judgment = response["judgments"][0]
    judgment["rationale"] = "A critic's rationale cannot replace the original claim."
    findings, ledger = verify_judgments(
        {proposal.fingerprint: proposal},
        ReviewVerification.model_validate_json(json.dumps(response)), context, snapshot)
    assert findings[0].status == "supported"
    assert findings[0].claim == proposal.claim and findings[0].reason == proposal.reason
    assert ledger[0]["judgments"][0]["counterargument"]
    assert ledger[0]["judgments"][0]["citations"][0]["source_digest"] == context[0]["digest"]


def test_duplicate_or_missing_judgments_abstain_and_unknown_proposals_are_ignored(tmp_path):
    snapshot, context, proposal, response = evidence(tmp_path)
    duplicate = copy.deepcopy(response["judgments"][0])
    duplicate["status"] = "refuted"
    unknown = {**duplicate, "fingerprint": "0" * 64}
    response["judgments"].extend([duplicate, unknown])
    findings, ledger = verify_judgments(
        {proposal.fingerprint: proposal},
        ReviewVerification.model_validate_json(json.dumps(response)), context, snapshot)
    assert findings[0].status == "needs_evidence"
    assert ledger[0]["reason"] == "conflicting_or_duplicate_judgments"
    assert ledger[-1]["reason"] == "unknown_proposal"
    findings, ledger = verify_judgments({proposal.fingerprint: proposal}, ReviewVerification(),
                                      context, snapshot)
    assert findings[0].status == "needs_evidence" and ledger[0]["reason"] == "missing_judgment"


def test_exact_cross_lane_duplicates_collapse_without_merging_distinct_triggers(tmp_path):
    snapshot, context, proposal, _ = evidence(tmp_path)
    repeat = proposal.model_copy(update={"category": "security", "severity": "critical"})
    distinct = proposal.model_copy(update={"trigger": "subtotal < 50", "expected": "5"})
    proposals, ledger = select_proposals([proposal, repeat, distinct], snapshot, context)
    assert len(proposals) == 2 and repeat.fingerprint in proposals
    assert ledger == [{"fingerprint": proposal.fingerprint, "reason": "duplicate",
                       "duplicate_of": repeat.fingerprint}]
    assert all(f.status == "proposed" for f in proposals.values())


@pytest.mark.parametrize("first,second", [("value='A'", "value='a'"),
                                         ("value='a b'", "value='a  b'")])
def test_dedup_preserves_case_sensitive_and_whitespace_sensitive_trigger_literals(
        tmp_path, first, second):
    snapshot, context, proposal, _ = evidence(tmp_path)
    one = proposal.model_copy(update={"trigger": first})
    two = proposal.model_copy(update={"trigger": second})
    proposals, filtering = select_proposals([one, two], snapshot, context)
    assert len(proposals) == 2 and not filtering


@pytest.mark.parametrize("patch,expected", [
    ("@@ -1,2 +1,2 @@\n same\n-old\n+new\n", {2}),
    ("@@ -0,0 +1,2 @@\n+one\n+two\n", {1, 2}),
    ("@@ -1,2 +1,2 @@\n-old\n+new\n", set()),
    ("@@ -1 +1 @@\n-old\n+new\n@@ -4 +4 @@\n-a\n+b\n", {1, 4}),
    ("@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n", {1}),
    ("not a complete patch", set()),
])
def test_only_added_lines_in_complete_hunks_are_admissible(patch, expected):
    assert added_lines(patch) == expected


def test_legacy_cached_generation_is_not_reused_after_verification_rules_change(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    verifier = LocalVerifier(trusted=True, artifacts=artifacts)
    snapshot = forge.snapshot(policy, 17)
    previous = store.enqueue("acme", policy.repo, 17, "legacy", {})
    job = store.claim("legacy-worker")
    legacy_result = {"state": "review_complete", "verification_passed": False}
    legacy_key = digest_json({"pipeline": "v1", "snapshot": snapshot.identity,
                              "policy": policy.digest, "model": model.name,
                              "verifier": type(verifier).__name__, "image": ""})
    store.put_job_record(job.id, "legacy-worker", job.fence, "generation", legacy_key,
                         {"job_id": previous, "result": legacy_result})
    store.complete(previous, "legacy-worker", job.fence, legacy_result)
    store.enqueue("acme", policy.repo, 17, "new-rules", {})
    result = Pipeline(store, [policy], forge, model, verifier, artifacts).run_once("worker")
    assert result["state"] == "awaiting_approval" and model.calls
    assert not result.get("reused_generation")


def test_a_grounded_refutation_is_not_treated_as_a_supported_defect(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    def refute(payload):
        result = supported_judgments(payload)
        for judgment in result["judgments"]:
            judgment["status"] = "refuted"
        return result
    model.responses["refute"] = refute
    store.enqueue("acme", policy.repo, 17, "refutation", {})
    result = Pipeline(store, [policy], forge, model,
                      LocalVerifier(trusted=True, artifacts=artifacts), artifacts).run_once("worker")
    assert result["state"] == "review_complete" and "repair" not in model.calls
    assert result["findings"][0]["status"] == "refuted"
