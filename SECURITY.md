# Security policy

## Supported version

Security fixes are provided for the current `2.x` release line. Older `1.x`
releases are not supported.

## Reporting a vulnerability

Please do not open a public issue for an undisclosed vulnerability. Report it
through GitHub private vulnerability reporting for this repository. If that is
not available, contact `com.arkfarin@gmail.com` with the affected version,
reproduction steps, and expected impact.

## Accepted documentation build risk

As of 2026-08-31, the documentation build has two accepted high-severity
advisories inherited through Docusaurus:

- `GHSA-w3rx-r6r6-pgpr`: denial of service in the `image-size` ICNS parser.
- `GHSA-5p2g-fcmc-qvqq`: denial of service in the `image-size` JXL and HEIF
  parsers.

No patched `image-size` release is available. This dependency is used only by
the documentation build and is not included in the CLI or its release bundles.
The repository contains none of the affected formats, and `pnpm check:images`
rejects ICNS, JXL, HEIF, and HEIC inputs before the Docusaurus build. The two
GHSA identifiers are the only audit exceptions. Remove the exceptions and the
format guard after Docusaurus resolves to a patched `image-size` version.
