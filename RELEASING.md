# Releasing twinflame

CI/CD is GitHub Actions. Two workflows:

- **`.github/workflows/tests.yml`** — runs the test matrix (Python 3.11 / 3.12 / 3.13)
  on every pull request. This is the gate you require before merging.
- **`.github/workflows/release.yml`** — on every push to `main` (i.e. a merged PR):
  re-runs the tests, then **if the version in `pyproject.toml` is not already tagged**,
  builds the sdist + wheel, publishes to PyPI, and creates the `vX.Y.Z` git tag +
  GitHub Release. A merge that doesn't bump the version is a no-op release.

## The version is single-sourced

`[project].version` in `pyproject.toml` is the only place to edit. `twinflame.__version__`
reads it back from the installed package metadata, so the two can't drift.

## Per-release flow

1. In your feature branch, bump `version` in `pyproject.toml` (e.g. `0.1.0b1` → `0.1.0`).
   Follow [PEP 440](https://peps.python.org/pep-0440/): `1.2.3`, `1.2.3b1`, `1.2.3rc1`, …
2. Open a PR. `tests.yml` runs the matrix — it must pass to merge.
3. Merge to `main`. `release.yml` runs: tests again → build → publish to PyPI →
   tag `vX.Y.Z` → GitHub Release with auto-generated notes.

If you merge **without** bumping the version, nothing is published (the tag already exists) —
so ordinary merges are safe.

## One-time setup

### 1. Branch protection (makes tests a hard gate)
Settings → Branches → add a rule for `main`:
- Require a pull request before merging.
- Require status checks to pass → select the **test** checks from `tests.yml`.

### 2. PyPI Trusted Publishing (no API token stored)
The release workflow authenticates to PyPI via OIDC — there is **no secret to manage**.
On <https://pypi.org> → *Your projects* → *Publishing* (or *Add a pending publisher* if the
`twinflame` project does not exist yet), add a GitHub publisher:

| Field | Value |
|---|---|
| Owner | `ankorio` |
| Repository | `twinflame` |
| Workflow name | `release.yml` |
| Environment | *(leave blank)* |

That's it — the first push to `main` with a new version will create/populate the project.

> **Name availability:** publishing requires the `twinflame` name to be free (or already
> owned by you) on PyPI. Check <https://pypi.org/project/twinflame/> first; if taken, rename
> the project in `pyproject.toml` and update the trusted publisher.

### Optional hardening: a `pypi` environment
For an extra approval gate, create a GitHub Environment named `pypi` (Settings →
Environments) with required reviewers, set `Environment: pypi` in the PyPI trusted publisher,
and add `environment: pypi` to the `release` job in `release.yml`. Then each publish waits for
a reviewer. Left off by default to keep releases hands-free.

### Optional: token fallback instead of Trusted Publishing
If you can't use OIDC, create a PyPI API token, store it as the `PYPI_API_TOKEN` repo secret,
and change the publish step in `release.yml` to:

```yaml
      - name: Publish to PyPI
        if: steps.ver.outputs.new == 'true'
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          password: ${{ secrets.PYPI_API_TOKEN }}
```
