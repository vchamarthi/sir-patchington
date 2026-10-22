# Example review CI section

Synthetic data from mocked GitHub responses; no real workflow was inspected.

<details><summary>CI status and failures</summary>

<p>Commit: <code>bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</code><br>
Observed: <code>2026-10-22T14:00:00+00:00</code> via offline fixture (not a real GitHub run)<br>
CI state: <strong>failure</strong>; coverage: bounded observation complete.</p>
<p>Workflow <code>CI</code>: completed / failure</p>
<p>Job <code>pytest</code>: completed / failure<br>Reason: Failed step(s): Run tests<br>Diagnosis (observed failure): A test reported an assertion failure; the underlying code cause is unknown<br>Next action: Reproduce the named failing test and compare its expected behavior to the code; do not change test expectations without evidence.</p><pre>AssertionError: 1 != 2
Process exit code 1</pre>
<p>Check <code>pytest</code>: completed / failure<br>Observed evidence: AssertionError: 1 != 2</p>
<p>Status <code>external</code>: success — </p>
<p>Limitation: CI observations are for this exact head commit; required-check coverage is not proven.</p>
<p>Limitation: Only static CI results are inspected; diagnosis is evidence-based and may remain unknown.</p>
<p>Limitation: Logs are bounded error excerpts with credential-pattern filtering, not complete DLP.</p>
</details>
