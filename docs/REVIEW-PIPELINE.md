# Source-grounded review pipeline

Sir Patchington selects revision-scoped context, collects specialist proposals,
filters their scope and duplicates, and requests an adversarial judgment for each
remaining proposal. Deterministic checks admit only source-grounded judgments.
Executable repair verification remains a separate authority boundary.

The design follows the context-selection and verification lessons in CodeRabbit's
[architecture documentation](https://docs.coderabbit.ai/overview/architecture)
and [context engineering disclosure](https://www.coderabbit.ai/blog/the-art-and-science-of-context-engineering).
It implements Sir Patchington's own controls; it does not reproduce CodeRabbit's
private hosted engine. An unpublished local REA investigation distinguishes
inspected client behavior from documented server behavior and unresolved
implementation details; that investigation is not included in this repository.

## Context selection

`GitWorkspace.source_slice` reads regular blobs from an exact commit SHA, without
executing repository programs, hooks or filters. Acquisition order is changed
files, their direct static Python imports, convention-matched tests and ancestor
guidelines. With an explicit read policy it also acquires a bounded set of safe
Python candidates across allowed directories, making unchanged callers and tests
discoverable. Source siblings are the final fallback.

The production pipeline uses `ContextIndex.retrieve_review` to select ranges,
not whole files. Complete unified-diff hunks identify added lines and anchors for
deletion-only changes. Hunk coverage and finding admission share the same
count-driven parser, so content resembling file headers stays inside its hunk.
Python AST functions/methods supply enclosing scope;
non-Python and unparseable source use hunk windows. Large functions are represented
by their signature and bounded windows rather than silently exhausting the pack.

The source budget has reserved shares, then unused space is redistributed:

| Priority | Selection evidence |
|---|---|
| Changed hunks: 45% | Original right-side line numbers; windows distributed across files |
| Guards: 10% | Static assertions, early exits, and named validation/authorization helpers |
| Tests: 20% | Test functions with resolved calls to changed functions |
| Callers: 10% | Function excerpts around resolved calls to changed functions |
| Function contracts: 10% | Enclosing functions, called helpers, imports, constants and class signatures |
| Guidance: 5% | Recognized ancestor guides and repository-wide Copilot instructions |

Each share uses round-robin file selection, exact UTF-8 byte accounting, and
overlap deduplication. Remaining space is reused in table order. Selection ranges,
reasons, added-line/hunk coverage, relationship candidates and omissions are
recorded and rendered in the review's Context coverage dropdown.

Caller resolution supports exact imported aliases, module-qualified calls,
lexical functions and `self`/`cls` method names. Shadowed imported names are not
borrowed from another scope. Unknown receivers, runtime dispatch, dynamic imports,
module-initialization calls and non-Python call graphs remain unresolved. Discovery
is bounded to acquired safe source, without transitive dependency expansion.
Selected tests are source context, not evidence that those tests executed. Guard
selection does not prove runtime dominance or exhaustive validation.

The pipeline supplies the trusted repository policy before acquisition.
`context_paths` and `context_exclusions` gate reads; root guides, singular `test/`
and `.github/copilot-instructions.md` require explicit allowlisting when the
defaults do not cover them. Credential-looking filenames and patterned credential
content are withheld. This filtering can reject benign examples and cannot detect
every secret. It protects model-bound context, not every stored record.

Acquisition selects at most 2 MB, further bounded by the requested budget and
`max_source_bytes`; it reads at most 200 blobs and 20 MB of raw blob content,
including discarded content. Tree inspection is bounded at 100,000 regular files.
Selected source ranges total at most `min(100,000, max_source_bytes)` bytes, with
original line endings counted once. Model serialization adds numbered lines,
snippet metadata and bounded PR/CI context; the existing 500 KB model-input cap
still applies. Canonical full source and its SHA-256 remain local for verification;
only selected ranges enter the model view. A small entirely selected file may
retain its legacy full-content field. Omitted ranges cannot be proposed finding
anchors or citations, even if their text exists in local canonical source.
Missing, partial or truncated changed-source/owner context still prevents
automatic repair and requires owner investigation. The legacy full-file retrieval
API remains for explicitly indexed cross-repository/compatibility consumers;
production PR review uses the excerpt selector.

## Finding admission and false-positive filtering

1. Specialist lanes propose concrete defects with a path, line, triggering input,
   expected behavior and rationale. Routing can add security, compatibility or
   native lanes; it does not grant execution authority.
2. `select_proposals` rejects locations outside changed files, outside added
   right-side diff lines, absent from selected source, or beyond the source range.
   Incomplete or malformed hunks produce no eligible added-line set.
3. Duplicate proposals are collapsed only when path, line, claim, trigger and
   expected behavior all match after trimming outer whitespace. Case, interior
   whitespace, punctuation and operators remain significant; there is no fuzzy or
   semantic similarity matching. Severity ordering chooses the retained repeat.
   Distinct locations, triggers or outcomes remain separate proposals. Reworded
   duplicates can survive this deliberately conservative rule.
4. The `refute` stage receives immutable proposals identified by fingerprint. It
   must state a counterargument and return `supported`, `refuted`, `needs_evidence`
   or `requires_owner`; it cannot replace the original claim.
5. `verify_judgments` checks every citation against selected tenant/repository/
   revision-scoped source: SHA-256 digest, ordered bounded line range and exact
   whole-line quote. A `supported` or `refuted` judgment needs valid citations,
   including one covering the finding's line. Missing, duplicate, conflicting or
   unverifiable judgments become `needs_evidence`; unknown fingerprints are ignored.

Only admitted `supported` findings can be reported or repaired. Unresolved
judgments block automatic repair. Review and filtering records retain the
decisions, admitted proposal fields and verification evidence. The pipeline version is
included in generation reuse, and older review records cannot bypass the new
report-publication gate.

## Two kinds of verification

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| Review citations and counterarguments | A judgment refers to the supplied pinned source and immutable proposal | Logical truth, executed behavior, or reviewer/verifier model independence |
| Baseline/candidate acceptance checks | Trusted configured commands executed against the exact original and candidate commits with recorded outcomes | General correctness outside those checks |
| GitHub CI observation | CI evidence associated with the captured repository and head SHA | Candidate repair success, model independence, or publication approval |

The review and adversarial stages use the configured model adapter. A second
model call is not independent execution. CI reports enter the review payload as
untrusted evidence; the pipeline validates repository/head identity and retains
the observation. Forge collectors and publication controls are documented
separately.

After review admission, a repair must produce a policy-permitted candidate against
the captured head. The independent executable verifier requires mandatory checks
and a change-specific regression check that fails on the original head and passes
on the candidate. It rejects acceptance-input changes, missing execution, all-skipped
tests, source mutation, timeouts, log overflow and failed candidate checks. Missing
required risk lanes also prevent verification from passing. Local execution is
explicitly for trusted fixtures; the Docker verifier supplies the isolated
execution path.

Passing repair evidence advances the candidate to `awaiting_approval`; it does
not authorize publication. Approval remains bound to the exact candidate,
evidence, policy and expected head, with reconstruction and publication readback
checks retained.

Behavioral fixture tests qualify these admission and authority controls. They do
not measure live-model precision, recall, false-positive reduction, production
scale or equivalence to CodeRabbit. Those quality claims require a separately
authorized evaluation using known defects and intentional non-defects.

Posted reports expose recorded coverage gaps before the findings and include a
collapsed limitations section. A limited review must not look complete just
because it has source citations or successful CI. The review-policy version
changes when prompt policy changes, invalidating older generations and reports.

Within the changed-file group, implementation source takes budget priority over
tests and documents, so large qualification records cannot displace runtime
code. The model receives an additional numbered source view. Original content
and digests remain canonical, and citations must match original lines without
number prefixes. The existing model-input limit still applies.
