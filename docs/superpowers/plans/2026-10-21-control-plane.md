# Sir Patchington Implementation Plan

> For agentic workers: use subagent-driven development for independent modules and verify each integration boundary.

**Goal:** a private, modular review-to-verified-repair repository with organization-safe control boundaries.

**Architecture:** PostgreSQL durable control plane and isolated replaceable workers. Credential-bound source/model access, untrusted execution and restricted publication remain separate roles. A strict immutable evidence contract connects modules.

**Tech stack:** Python 3.12+, FastAPI/Pydantic, SQLAlchemy/PostgreSQL, HTTPX/GitHub Apps, Git, Docker, pytest, uv lock.

## Global constraints

- No real model API/generation tests. Fixtures cannot be selected in production mode.
- No merge/auto-merge/default-branch push; publication disabled until repository policy enables it and exact tested candidate approval exists.
- All API and durable state lookups are tenant scoped; trusted policies are loaded outside reviewed repositories.
- No credentials in repository, execution sandbox, logs or model-generated commands.

## Tasks

- [x] Contracts and policy: write validation tests, run red, implement strict hashes/identities/path/command constraints, run green.
- [x] State module: test duplicate delivery, tenant isolation, expired lease fencing, same-PR exclusion and tenant capacity; implement transactional PostgreSQL queue and records; run SQLite and real PostgreSQL tests.
- [x] Workspace/evidence/execution: test immutable artifacts, unsafe patches, canonical identity, baseline/candidate evidence and source mutation; implement isolated Git and Docker verifier; run actual fixture Git/container checks.
- [x] GitHub/publisher: test scoped installation tokens, pagination, authoritative snapshots, stale state, evidence tampering, approval signatures and CAS publication; implement read transport and restricted publisher with fake HTTP/local bare-Git tests.
- [x] Intelligence/pipeline/API: test context provenance, mandatory risk lanes, structured model output, feedback triage, bounded repair, authenticated approvals and denied cross-team access; implement adapters and persisted worker workflow.
- [x] Operability: add migration command, health/readiness/metrics, reconciliation/outbox, locked dependency build, nonroot images, multi-replica deployment manifests, secret/config validation, runbooks and source provenance mapping.
- [x] Qualification: full tests, lint/build/audit, PostgreSQL concurrency/recovery, actual Docker baseline/candidate demonstration; independent code review; repair failures and repeat relevant checks.
- [x] Repository delivery: secret scan staged files, commit exact tested state, create private `vchamarthi/sir-patchington`, push, verify privacy and remote commit; verify GitHub CI if available. No changes to existing application repositories.
