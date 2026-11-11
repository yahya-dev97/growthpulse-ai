# Milestone 1 Report: Project Foundation

**Status:** complete, awaiting review. Milestone 2 has not started.

## Result

The project installs from its lockfile on Python 3.12.11, and linting, formatting, and all 24 tests pass. Nothing has been committed or pushed, and no external resources were created. The GitHub Actions workflow has not been executed, because it only runs once the code is on GitHub.

## Environment found

| Item | Finding |
|---|---|
| Workspace | Empty, not a Git repository |
| System Python | 3.9.6, too old and left unchanged |
| Project Python | 3.12.11, managed by uv 0.13.7 |
| Git | 2.39.5, user name and email configured |
| Also available | Homebrew, GitHub CLI, Supabase CLI (none used to create anything) |

## What was created

| Path | Purpose |
|---|---|
| `pyproject.toml`, `uv.lock`, `.python-version` | Project metadata, locked dependencies, Python pin |
| `.venv/` | Virtual environment (ignored by Git) |
| `src/growthpulse_ai/config.py` | Typed settings from environment variables, with secrets masked |
| `src/growthpulse_ai/{connectors,analytics,ai_analysis,persistence,pipeline,dashboard}/` | Empty packages that define the architecture |
| `tests/` | Smoke, configuration, and architecture-boundary tests |
| `.env.example` | Safe template, all credentials blank |
| `.gitignore` | Excludes credentials, local databases, virtual environments, and temporary artifacts |
| `.github/workflows/ci.yml` | Lint, format check, and tests on push and pull request |
| `README.md` | Goals, architecture, guardrails, setup, roadmap |
| `.git/` | Local repository on branch `main`, with no commits and no remote |

Runtime dependency: `pydantic-settings`. Development dependencies: `pytest` 9.2.0 and `ruff` 0.17.4. Streamlit, the Anthropic SDK, and database drivers are deferred to the milestones that use them.

## Commands run

```bash
# Inspection
git --version; python3 --version; uv --version; uv python list --only-installed

# Project setup
uv init --package --name growthpulse-ai --python 3.12 --vcs none --no-readme \
  --description "AI-powered performance marketing analysis platform" .
uv add pydantic-settings
uv add --dev pytest ruff
uv sync
git init -b main

# Verification
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run pytest
git check-ignore <paths>
git ls-files -co --exclude-standard
```

`git ls-remote` was also used to confirm that the action versions pinned in the CI workflow exist.

## Test results

Final run, executed in the project directory:

| Check | Result |
|---|---|
| `uv lock --check` | Lockfile up to date |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 13 files already formatted |
| `pytest` | **24 passed, 0 failed** |

Additional verification:

| Check | Result |
|---|---|
| Fresh install of only the files Git would commit, in a scratch directory | Lint, format, and 24 tests passed |
| `.gitignore`, 22 sensitive or temporary sample paths | All ignored |
| `.gitignore`, 8 required files including `.env.example` | None ignored |
| Pattern scan of files Git would commit for API keys, tokens, private keys, database URLs with passwords, and personal email | No matches |
| Added a forbidden dashboard-to-pipeline import in the scratch copy | Boundary test failed, as intended |
| Removed the protection against echoing rejected values in the scratch copy | Leak test failed, as intended |

Two problems were found and fixed along the way:

- One architecture test had a wrong expected count, which caused the only test failure during development.
- The first secret-leak test still passed with the protection removed, so it proved nothing. It was rewritten and confirmed to fail without the protection.

## Remaining dependencies

Needed only at the milestone shown. None is needed for Milestones 2 and 3.

| Dependency | Needed at | Requires your approval |
|---|---|---|
| Supabase project and database credentials | Milestone 4 | Yes |
| Anthropic API key (paid usage) | Milestone 5 | Yes |
| GitHub repository, Actions secrets | Milestone 7, or earlier if you want CI running | Yes |
| Streamlit hosting | Milestone 8 | Yes |
| Google Ads developer token and read-only OAuth access | Milestone 8 | Yes |
| Meta access token limited to `ads_read` | Milestone 8 | Yes |

## Decisions for you

1. **First commit.** Files are ready but uncommitted. Approve it, or make it yourself with `git add . && git commit`.
2. **License.** None has been added. A public portfolio repository without one is all rights reserved by default.

## Next steps

After your review, Milestone 2 defines the normalized advertising data schema and builds the synthetic data connector, with every generated record labeled as synthetic.
