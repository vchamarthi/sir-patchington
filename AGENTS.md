# Maintainer instructions

- Keep models outside the authority boundary: proposals cannot choose commands,
  policy, image, credentials, approval identities or publication targets.
- Default tests must not authenticate models, generate real model output, install
  GitHub Apps, push to existing repositories, or deploy infrastructure. Use
  fixture models, mocked HTTP, disposable Git repositories and isolated local
  PostgreSQL/Docker resources. Label simulations explicitly.
- Every external effect must validate tenant/repository/PR identity, current
  policy, live lease/fence, exact evidence and current approval where applicable.
  Preserve clean reconstruction, expected-head CAS, readback and no-merge policy.
- Keep read workers, execution containers and credentialed publishers separate.
  Do not forward host environment, home, auth files or sockets into checks.
- Regressions need behavioral failure evidence before changing code. Verify
  relevant tests and `uv run ruff check src tests deploy scripts`. Do not claim
  model quality, production scale or deployment isolation from fixture success.
- Update the qualification record when changing authority, evidence, execution
  images, dependency/client pins, or deployment semantics. Research ideas and
  unimplemented integrations must stay distinguishable from working code.
