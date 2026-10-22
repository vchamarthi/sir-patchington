"""Scripted model callbacks use numbered views; raw source still controls citation admission."""

import hashlib

from test_pipeline import FINDING, fixture

from patchington.pipeline import Pipeline


def test_numbered_model_context_supports_exact_original_source_citations(tmp_path):
    store, policy, forge, model, artifacts = fixture(tmp_path)
    policy = policy.model_copy(update={"repair_enabled": False})
    calls = []

    def review(payload):
        source = next(item for item in payload["context"] if item["path"] == FINDING["path"])
        numbered_line = next(line for line in source["numbered_content"].splitlines()
                             if "subtotal > 50" in line)
        number, original_line = numbered_line.split(": ", 1)
        assert source["content"].splitlines()[int(number) - 1] == original_line
        assert source["digest"] == hashlib.sha256(source["content"].encode()).hexdigest()
        assert "numbered_content" in payload["review_instruction"]
        calls.append((source["digest"], int(number), original_line))
        return {"findings": [{**FINDING, "line": int(number)}]}

    def verify(payload):
        proposal = payload["proposals"][0]
        source = next(item for item in payload["context"] if item["path"] == proposal["path"])
        numbered_line = source["numbered_content"].splitlines()[proposal["line"] - 1]
        number, original_line = numbered_line.split(": ", 1)
        assert "numbered_content" in payload["instruction"]
        assert (source["digest"], int(number), original_line) == calls[0]
        return {"judgments": [{"fingerprint": proposal["fingerprint"], "status": "supported",
            "rationale": "Source excludes the specified inclusive delivery threshold.",
            "counterargument": "An exclusive threshold would require another contract.",
            "citations": [{"path": source["path"], "source_digest": source["digest"],
                           "start_line": int(number), "end_line": int(number),
                           "quote": original_line}]}]}

    model.responses["review"], model.responses["refute"] = review, verify
    job = store.enqueue("acme", policy.repo, 17, "numbered-context", {})
    result = Pipeline(store, [policy], forge, model, None, artifacts).run_once("worker")
    assert result["state"] == "needs_owner" and result["reason"] == "unresolved findings or repair disabled"
    record = store.get_record("acme", "findings", job)
    assert record["findings"][0]["status"] == "supported"
    citation = record["verification"][0]["judgments"][0]["citations"][0]
    assert citation["start_line"] == 2 and citation["quote"].startswith("    return")
    assert citation["source_digest"] == calls[0][0]
    assert model.calls == ["review", "refute"]
