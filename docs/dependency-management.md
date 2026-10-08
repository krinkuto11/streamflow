# Dependency Management

## Dependabot policy

The active configuration is [`.github/dependabot.yml`](../.github/dependabot.yml)
on the default branch, `main`. Keep the same configuration on `dev` so future
releases preserve this policy.

| Updates | Target | Policy |
| --- | --- | --- |
| Frontend npm versions | `dev` | Check weekly on Monday at 10:00 Europe/Berlin; group runtime and development minor/patch updates separately; review major updates individually. |
| GitHub Actions versions | `dev` | Same weekly schedule; group minor/patch updates and review major updates individually. |
| Frontend / GitHub Actions security fixes | `main` | Dependabot security updates use the default branch; group minor/patch fixes separately by ecosystem and leave major fixes individual. |

Version PRs are limited to five for npm and three for GitHub Actions. Newly
published versions have a three-day cooldown; npm major versions have a seven-day
cooldown. Security updates have no version-update cooldown. Their configuration
entries omit `target-branch` and set `open-pull-requests-limit: 0` to suppress
regular version PRs against `main`; the zero limit does not disable security PRs.
The schedule in those entries is required configuration, not a delay on alerts
or security fixes.

There is no automatic merge. Dependency PRs must pass the existing Tests and
CodeQL workflows and receive compatibility review. After merging a security fix
into `main`, carry the relevant manifest/lockfile or workflow change into `dev`
so the next release retains the fix. Regular updates reach `main` through the
normal release process.

Repository administrators must keep Dependabot alerts and security updates
enabled in **Settings -> Advanced Security -> Dependabot**; the YAML config
does not enable those repository switches. See GitHub's
[configuration reference](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference)
and [security update guidance](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-security-updates).

## Python locks and media images

Python updates remain coordinated, reviewed lockfile updates. Dependabot's
current pip fetcher discovers `.txt` and `.in` requirements, and its pip-compile
integration pairs `.in` inputs with `.txt` outputs. StreamFlow instead has
human-edited `.txt` inputs and custom `.lock` outputs. Enabling a plain `pip`
entry would not maintain the production and test locks installed by Docker and
CI. Keep both locks in sync using the procedure below; CI continues to audit the
production lock with `pip-audit`. Sources: Dependabot's
[pip file discovery](https://github.com/dependabot/dependabot-core/blob/main/python/lib/dependabot/python/shared_file_fetcher.rb)
and [pip-compile lock matching](https://github.com/dependabot/dependabot-core/blob/main/python/lib/dependabot/python/pip_compile_file_matcher.rb).

Docker base images and FFmpeg/ffprobe pins also remain manually reviewed because
the media toolchain must retain its tested compatibility with Dispatcharr.

## Regenerating Python locks

StreamFlow keeps human-edited Python inputs separate from generated install locks:

- `backend/requirements.txt` lists production dependencies.
- `backend/requirements-dev.txt` lists test and audit tooling.
- `backend/requirements.lock` is the hashed Python 3.11 production lock used by the image.
- `backend/requirements-test.lock` is the hashed Python 3.11 CI/test lock.

Regenerate both locks with Python 3.11 and `pip-tools 7.5.3`:

```bash
python -m pip install pip-tools==7.5.3
python -m piptools compile backend/requirements.txt \
  --output-file backend/requirements.lock --generate-hashes --strip-extras \
  --resolver backtracking --newline lf --no-emit-index-url --no-emit-trusted-host \
  --allow-unsafe --upgrade
python -m piptools compile backend/requirements.txt backend/requirements-dev.txt \
  --output-file backend/requirements-test.lock --generate-hashes --strip-extras \
  --resolver backtracking --newline lf --no-emit-index-url --no-emit-trusted-host \
  --allow-unsafe --upgrade
```

`--allow-unsafe` is intentional: the production input pins `setuptools` so the
container replaces vulnerable vendored build tooling inherited from the Python
base image, and both lock files must retain that reviewed version and its hashes.

Verify a regenerated lock before committing it:

```bash
python -m pip install --require-hashes -r backend/requirements-test.lock
pip-audit -r backend/requirements.lock
python -m pytest backend/tests -m "not integration and not live" -q
python -m pytest backend/tests -m "integration and not live" -q
```

Frontend installs use `npm ci` and `frontend/package-lock.json`. Run `npm audit
--audit-level=high`, the complete frontend test suite, and the production build
after dependency updates.
