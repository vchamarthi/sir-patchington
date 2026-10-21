import hashlib
import json

import pytest


def test_artifacts_are_content_addressed_immutable_and_canonical(tmp_path):
    from patchington.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path)
    digest = store.put(b"evidence")
    assert digest == hashlib.sha256(b"evidence").hexdigest()
    assert store.get(digest) == b"evidence"
    assert store.put(b"evidence") == digest
    assert store.put_json({"b": 2, "a": 1}) == store.put_json({"a": 1, "b": 2})
    assert store.get_json(store.put_json({"a": 1})) == {"a": 1}


def test_artifacts_refuse_traversal_corruption_symlinks_and_nonobjects(tmp_path):
    from patchington.artifacts import ArtifactError, ArtifactStore

    store = ArtifactStore(tmp_path / "evidence")
    for digest in ("../outside", "f" * 63, "A" * 64, "f" * 65):
        with pytest.raises(ArtifactError):
            store.get(digest)
    digest = store.put(b"original")
    path = store.root / digest[:2] / digest[2:]
    path.chmod(0o600)
    path.write_bytes(b"corrupt")
    with pytest.raises(ArtifactError, match="digest"):
        store.get(digest)
    other = hashlib.sha256(b"secret").hexdigest()
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    (store.root / other[:2]).mkdir(exist_ok=True)
    (store.root / other[:2] / other[2:]).symlink_to(outside)
    with pytest.raises(ArtifactError):
        store.get(other)
    with pytest.raises(ArtifactError):
        store.get_json(store.put(json.dumps([1, 2]).encode()))
