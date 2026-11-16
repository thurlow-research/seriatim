# Contributing to Seriatim

Thank you for your interest.

- **Bug fixes are welcome as pull requests.** Describe the bug, how to reproduce it, and how
  the fix was tested. Include a test that fails without the fix.
- **Features start as an issue.** Open an issue that describes the problem and the proposed
  change before writing code. Design changes go through `docs/` (the SPEC, the ADRs and the
  technical designs) before implementation.
- **CI is the authority.** A pull request must pass every CI check, including the secret
  scan. Paste command output rather than writing "tests pass".
- **No secrets, host names or IP addresses** in code, docs, tests or fixtures. Configuration
  comes from environment variables or a local config file. Only `.env.example` is committed.
  Install the pre-commit hook once with `pre-commit install`; it runs gitleaks before each
  commit.
- Small, focused pull requests are easier to review.

By contributing, you agree that your contributions are licensed under the MIT licence of this
repository.
