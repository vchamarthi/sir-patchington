"""Behavioral evidence for bounded excerpts; no live model or GitHub calls."""

import copy
import hashlib
import json
import subprocess
import sys

from test_context_selection import policy, snapshot

from patchington.context import ContextIndex, model_context
from patchington.contracts import ChangedFile, Finding, ReviewVerification
from patchington.review import select_proposals, verify_judgments


def change(source, path, line):
    return ChangedFile(path=path, patch=f"@@ -{line} +{line} @@\n-old\n+"
                       + source.splitlines()[line - 1] + "\n")


def select(files, changes, budget=4_000):
    p, s, index = policy(), snapshot(), ContextIndex()
    s = s.model_copy(update={"files": tuple(changes)})
    index.add(s, files, p)
    rows = index.retrieve_review(s, p, budget)
    return rows, index.report(s, p), s


def visible(row, line):
    return any(r["start_line"] <= line <= r["end_line"] for r in row["selected_ranges"])


def test_large_changed_files_supply_hunks_without_shipping_whole_files():
    files = {f"src/{name}.py": "# FILLER\n" * 1_000 +
             f"def {name}(value):\n    return value + 1\n" for name in ("a", "b", "c")}
    rows, report, _ = select(files, [change(text, path, 1_002)
                                    for path, text in files.items()], budget=900)
    assert {r["path"] for r in rows} == set(files)
    assert all(visible(r, 1_002) for r in rows)
    assert 0 < report["retrieved_bytes"] <= 900
    viewed = model_context(rows)
    assert all("1002:     return value + 1" in r["numbered_content"] for r in viewed)
    assert all("content" not in r for r in viewed)
    assert "FILLER" not in json.dumps(viewed)
    assert report["changed_lines"]["selected"] == report["changed_lines"]["total"] == 3


def test_context_includes_guards_exact_alias_callers_and_tests():
    files = {
        "src/pay/fees.py": "def validate(amount):\n    if amount < 0:\n"
        "        raise ValueError('negative')\n\ndef fee(amount):\n    validate(amount)\n"
        "    if amount == 0:\n        return 0\n    return amount * 2\n",
        "src/checkout/order.py": "from pay.fees import fee as calculate\n\n"
        "def checkout(amount):\n    return calculate(amount)\n",
        "tests/test_fees.py": "from pay.fees import fee\n\ndef test_zero():\n"
        "    assert fee(0) == 0\n",
        "src/unrelated.py": "def fee(value):\n    return value\n\n"
        "def unrelated():\n    return fee(3)\n",
    }
    rows, report, _ = select(files, [change(files['src/pay/fees.py'], 'src/pay/fees.py', 9)])
    by_path = {r["path"]: r for r in rows}
    assert set(by_path) == set(files) - {"src/unrelated.py"}
    assert "guard" in by_path['src/pay/fees.py']['selection_reasons']
    assert visible(by_path['src/pay/fees.py'], 2)
    assert "caller" in by_path['src/checkout/order.py']['selection_reasons']
    assert "test" in by_path['tests/test_fees.py']['selection_reasons']
    assert report['relationships']['caller']['selected'] >= 1
    assert report['relationships']['test']['selected'] >= 1


def test_early_guard_outside_a_large_function_hunk_is_retained():
    source = ('def fee(amount):\n    if amount < 0:\n        raise ValueError("negative")\n'
              + '    amount += 1\n' * 400 + '    return amount * 2\n')
    rows, _, _ = select({'src/pay.py': source}, [change(source, 'src/pay.py', 404)], budget=600)
    assert visible(rows[0], 2) and visible(rows[0], 404)
    assert not visible(rows[0], 200)
    assert 'guard' in rows[0]['selection_reasons']


def test_large_caller_contributes_preconditions_far_before_its_call():
    source = 'def fee(amount):\n    return amount * 2\n'
    caller = ('from pay import fee\n\ndef checkout(amount):\n'
              '    if amount < 0:\n        raise ValueError("negative")\n'
              + '    amount += 1\n' * 300 + '    return fee(amount)\n')
    rows, _, _ = select({'src/pay.py': source, 'src/cart.py': caller},
                         [change(source, 'src/pay.py', 2)], budget=1_000)
    row = next(r for r in rows if r['path'] == 'src/cart.py')
    assert visible(row, 4) and visible(row, 5) and visible(row, 306)
    assert not visible(row, 150)
    assert 'guard' in row['selection_reasons']


def test_test_budget_is_reserved_despite_many_changed_lines():
    source = "def compute(value):\n" + "    value += 1\n" * 500 + "    return value\n"
    test = "from pay import compute\n\ndef test_boundary():\n    assert compute(0) == 500\n"
    rows, report, _ = select({'src/pay.py': source, 'tests/test_pay.py': test},
        [ChangedFile(path='src/pay.py', patch='@@ -0,0 +1,502 @@\n' +
                     ''.join('+' + line + '\n' for line in source.splitlines()))], budget=1_200)
    assert any(r['path'] == 'tests/test_pay.py' for r in rows)
    assert report['changed_lines']['selected'] < report['changed_lines']['total']
    assert report['retrieved_bytes'] <= 1_200


def test_shadowed_import_does_not_invent_a_caller():
    files = {'src/pay.py': 'def fee(x):\n    return x + 1\n',
             'src/other.py': 'from pay import fee\n\ndef unrelated(fee):\n    return fee(1)\n'}
    rows, _, _ = select(files, [change(files['src/pay.py'], 'src/pay.py', 2)])
    assert [r['path'] for r in rows] == ['src/pay.py']


def test_nested_definition_shadows_an_imported_name():
    files = {'src/pay.py': 'def fee(x):\n    return x + 1\n',
             'src/other.py': 'from pay import fee\n\ndef unrelated():\n'
             '    def fee(x):\n        return x\n    return fee(1)\n'}
    rows, _, _ = select(files, [change(files['src/pay.py'], 'src/pay.py', 2)])
    assert [r['path'] for r in rows] == ['src/pay.py']


def test_enclosing_parameter_prevents_borrowing_a_global_import_in_a_closure():
    files = {'src/pay.py': 'def fee(x):\n    return x + 1\n',
             'src/other.py': 'from pay import fee\n\ndef outer(fee):\n'
             '    def unrelated():\n        return fee(1)\n    return unrelated()\n'}
    rows, _, _ = select(files, [change(files['src/pay.py'], 'src/pay.py', 2)])
    assert [r['path'] for r in rows] == ['src/pay.py']


def test_nested_validation_helper_is_resolved_in_the_current_function_scope():
    source = ('def fee(amount):\n    def validate(value):\n        if value < 0:\n'
              '            raise ValueError("negative")\n    validate(amount)\n'
              + '    amount += 1\n' * 300 + '    return amount * 2\n')
    rows, _, _ = select({'src/pay.py': source}, [change(source, 'src/pay.py', 306)], budget=700)
    assert visible(rows[0], 3) and visible(rows[0], 4) and visible(rows[0], 306)


def test_new_file_hunk_discovers_each_changed_function_after_module_imports():
    source = 'import math\n\ndef fee(x):\n    return math.ceil(x)\n'
    files = {'src/pay.py': source, 'tests/test_pay.py':
             'from pay import fee\n\ndef test_fee():\n    assert fee(1.5) == 2\n'}
    patch = '@@ -0,0 +1,4 @@\n' + ''.join('+' + line + '\n' for line in source.splitlines())
    rows, report, _ = select(files, [ChangedFile(path='src/pay.py', patch=patch)])
    assert 'tests/test_pay.py' in {r['path'] for r in rows}
    assert report['relationships']['test']['candidates'] > 0


def test_decorator_hunk_selects_the_decorated_function_and_its_tests():
    source = 'def authorize(fn):\n    return fn\n\n@authorize\ndef fee(x):\n    return x + 1\n'
    files = {'src/pay.py': source, 'tests/test_pay.py':
             'from pay import fee\n\ndef test_fee():\n    assert fee(1) == 2\n'}
    rows, _, _ = select(files, [change(source, 'src/pay.py', 4)])
    assert 'tests/test_pay.py' in {r['path'] for r in rows}


def test_out_of_range_diff_lines_are_bounded_and_cannot_hang_selection():
    program = '''
from patchington.context import ContextIndex
from patchington.contracts import ChangedFile, ChangeSnapshot, RepoPolicy
p = RepoPolicy(tenant="acme", team="dev", installation_id=1, repository_id=2, repo="acme/shop")
s = ChangeSnapshot(tenant="acme", installation_id=1, repository_id=2, repo="acme/shop", pr=1,
    base_sha="a" * 40, head_sha="b" * 40, head_ref="feature", head_repo="acme/shop",
    files=(ChangedFile(path="src/a.py", patch="@@ -999 +999 @@\\n-old\\n+new\\n"),))
i = ContextIndex()
i.add(s, {"src/a.py": "value = 1\\n"}, p)
rows = i.retrieve_review(s, p, 1000)
assert i.report(s,p)["changed_lines"]["selected"] == 0
'''
    subprocess.run([sys.executable, '-c', program], check=True, capture_output=True, timeout=2)


def test_hunks_work_for_non_python_and_deletion_only_changes():
    files = {'src/a.ts': '// untouched\n' * 200 + 'export const value = 2;\n',
             'src/b.py': '# unchanged\n' * 200 + 'value = 1\n'}
    changes = [change(files['src/a.ts'], 'src/a.ts', 201),
               ChangedFile(path='src/b.py', patch='@@ -201,2 +201 @@\n value = 1\n-old\n')]
    rows, report, _ = select(files, changes, budget=600)
    assert {r['path'] for r in rows} == set(files)
    assert all(visible(r, 201) for r in rows)
    assert report['changed_hunks']['with_context'] == 2


def test_omitted_lines_cannot_be_cited_or_admitted_even_when_in_canonical_source():
    source = ('def fee(x):\n    return x + 1\n' + '# elsewhere\n' * 200
              + 'def elsewhere():\n    return 3\n')
    rows, _, s = select({'src/pay.py': source}, [change(source, 'src/pay.py', 2)], budget=400)
    assert rows and not visible(rows[0], 203)
    proposal = Finding(path='src/pay.py', line=2, severity='medium', category='correctness',
                       claim='wrong result', trigger='x=1', expected='1', reason='boundary')
    verification = ReviewVerification.model_validate_json(json.dumps({'judgments': [{
        'fingerprint': proposal.fingerprint, 'status': 'supported', 'rationale': 'claim',
        'counterargument': 'No counterexample supplied', 'citations': [
            {'path': 'src/pay.py', 'source_digest': rows[0]['digest'],
             'start_line': n, 'end_line': n, 'quote': source.splitlines()[n - 1]}
            for n in (2, 203)]}]}))
    findings, ledger = verify_judgments({proposal.fingerprint: proposal}, verification, rows, s)
    assert findings[0].status == 'needs_evidence'
    assert ledger[0]['reason'] == 'unverifiable_source_citations'
    elsewhere = proposal.model_copy(update={'line': 203})
    s = s.model_copy(update={'files': (change(source, 'src/pay.py', 203),)})
    admitted, rejected = select_proposals([elsewhere], s, rows)
    assert not admitted and rejected[0]['reason'] == 'source_line_not_selected'


def test_excerpts_preserve_digest_and_original_line_numbers_without_mutation():
    source = '# elsewhere\n' * 200 + 'def fee(x):\n    return x + 1\n'
    rows, _, _ = select({'src/pay.py': source}, [change(source, 'src/pay.py', 202)], budget=300)
    before = copy.deepcopy(rows)
    view = model_context(rows)[0]
    assert view['digest'] == hashlib.sha256(source.encode()).hexdigest()
    assert view['snippets'][0]['start_line'] > 1
    assert '202:     return x + 1' in view['numbered_content']
    assert sum(len(s['content'].encode()) for s in view['snippets']) <= 300
    view['snippets'][0]['content'] = 'model mutation'
    assert rows == before


def test_zero_budget_and_utf8_accounting_are_deterministic():
    text = 'def fee(x):\n    return "é" * x\n'
    files, changes = {'src/pay.py': text}, [change(text, 'src/pay.py', 2)]
    rows, report, _ = select(files, changes, budget=0)
    assert rows == [] and report['retrieved_bytes'] == 0
    a, ar, _ = select(files, changes, budget=len(text.encode()))
    b, br, _ = select(files, changes, budget=len(text.encode()))
    assert a == b and ar == br and ar['retrieved_bytes'] == len(text.encode())


def test_pipeline_records_excerpt_coverage_and_passes_it_to_reporter(tmp_path):
    from test_review_ci_pipeline import engine

    store, p, forge, model, pipeline = engine(tmp_path)
    job = store.enqueue('acme', p.repo, 17, 'excerpt-coverage', {})
    assert pipeline.run_once('reviewer')['state'] == 'review_complete'
    record = store.get_record('acme', 'findings', job)
    assert record['coverage']['strategy'] == 'function-hunk-v1'
    assert record['coverage']['changed_lines'] == {'total': 1, 'selected': 1}
    posted = []
    forge.post_review = lambda *args, **kw: posted.append(kw['review_coverage']) or '1'
    assert pipeline.run_once('publisher', queue='publish')['state'] == 'review_reported'
    assert posted == [record['coverage']]


def test_coverage_dropdown_reports_ranges_and_escapes_paths():
    from patchington.review_context import render_review_coverage

    source = 'def fee(x):\n    return x + 1\n'
    _, report, _ = select({'src/pay.py': source}, [change(source, 'src/pay.py', 2)])
    report['selections'][0]['path'] = 'src/<img>\n\n[link](https://example.invalid).py'
    report['source_slice_omissions'] = {'tests/test_ci.py': 'credential_signature'}
    rendered = render_review_coverage(report)
    assert '<details><summary>Context coverage</summary>' in rendered
    assert '1 / 1' in rendered and '1–2' in rendered
    assert '<img>' not in rendered and '&lt;img&gt;' in rendered
    assert '\n\n[link]' not in rendered
    assert 'tests/test_ci.py' in rendered and 'credential_signature' in rendered


def test_coverage_from_another_revision_never_reaches_github():
    import httpx
    import pytest
    from test_github import fixture_handler, policy

    from patchington.github import GitHubClient, GitHubError

    calls = []
    def tokens(*args):
        return 'fixture-token'
    tokens.app_id = 42
    def handle(request):
        calls.append(request.method)
        return fixture_handler(request)
    client = GitHubClient(tokens, transport=httpx.MockTransport(handle))
    p = policy(post_reviews=True)
    s = client.snapshot(p, 7)
    calls.clear()
    with pytest.raises(GitHubError, match='coverage belongs to another'):
        client.post_review(p, s, (), review_coverage={
            'revision': 'c' * 40, 'repository_id': p.repository_id, 'repo': p.repo})
    assert calls == []
