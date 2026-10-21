import json

from patchington import cli


def test_continuous_worker_reconciles_periodically_and_survives_one_discovery_failure(
    monkeypatch, capsys
):
    now, events, discoveries, claims = [0.0], [], [], []

    class Event:
        stopped = False

        def __init__(self):
            events.append(self)

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            now[0] += 31
            return self.stopped

    class Worker:
        def run_once(self, owner, queue):
            claims.append(queue)
            if len(claims) == 6:
                events[0].set()
            return None

    def reconcile():
        discoveries.append(now[0])
        if len(discoveries) == 1:
            raise RuntimeError("private-error-text-must-not-leak")
        return {"status": "reconciled", "discovered": 0, "job_ids": [], "failures": []}

    monkeypatch.setattr(cli.threading, "Event", Event)
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
    assert (
        cli._run_queues(Worker(), "worker", False, reconcile=reconcile, reconcile_interval=30) == 0
    )
    assert discoveries == [0, 31, 62]
    assert claims == ["review", "observe"] * 3
    output = capsys.readouterr().out
    assert "private-error-text" not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert records[0]["reconciliation"]["error"] == "RuntimeError"
    assert records[-1] == {"status": "stopped", "role": "worker"}
