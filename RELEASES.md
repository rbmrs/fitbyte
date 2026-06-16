# Releases

Fitbyte publishes unsigned macOS beta builds on the GitHub [Releases page](https://github.com/rbmrs/fitbyte/releases).

## Downloading

1. Open the latest pre-release.
2. Download `Fitbyte-*-macos.zip`.
3. Unzip it and move `Fitbyte.app` wherever you keep apps.
4. On first launch, right-click `Fitbyte.app` and choose **Open** to bypass Gatekeeper for the unsigned beta build.

## Publishing

Releases are automated by `.github/workflows/release.yml`.

Every qualifying push to `main`:

1. Computes the next `0.1.0-beta.N` version.
2. Builds the macOS app with `scripts/build_macos_app.sh release <version>`.
3. Packages `Fitbyte.app` as `Fitbyte-<version>-macos.zip`.
4. Creates a GitHub pre-release with generated notes.

Docs-only pushes are ignored so README and article updates do not create empty beta builds.

To test the build locally:

```bash
scripts/build_macos_app.sh
open .build/debug/Fitbyte.app
```

The current beta line is intentionally unsigned. Code signing, notarization, and a stable non-beta release should happen before wider distribution.
