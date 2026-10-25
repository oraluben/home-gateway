# Maintenance conventions

- Keep the existing version in `versions.json` during routine fixes, refactors,
  and validation deployments. Group version changes around a deliberate release;
  do not bump the version for each edit or deployment.
- Distinguish deployments using their image ID and build recipe. Deployment tags
  remain immutable even when the human-readable version stays the same.
