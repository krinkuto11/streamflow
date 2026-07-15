# Dependency Management

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
