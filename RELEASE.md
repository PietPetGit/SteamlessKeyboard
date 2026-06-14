# Release checklist

1. Start from a clean working tree.
2. Update user-facing docs if behavior or dependencies changed.
3. Run a source sanity check:

   ```bash
   python -m compileall windows linux
   ```

4. Build locally on the target platform when possible:

   ```bash
   cd windows && python build.py
   cd linux && python build.py
   ```

5. Create and push a version tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

The `Build release assets` GitHub Actions workflow builds the Windows zip and
Linux tarball and attaches them to the tagged GitHub release.
