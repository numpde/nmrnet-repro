# Checker dependency preparation

The checker image is dedicated to offline repository proof. Unit/archive lanes
keep the existing Python base. Acquisition is explicit and uses `tools/docker.sh`
with its admitted interface and loopback proxy; image installation uses only
verified local artifacts with build networking and pulling disabled.

The declaration is `checker-dependencies.json`, with schema, exact base
digest, `linux/amd64`, and a closed package inventory. Every entry owns name,
version, filename, official URL, byte size and SHA-256. The existing pinned base
is Python 3.12.12 slim Bookworm at digest
`593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c`.

The direct tools are Debian Git `1:2.39.5-0+deb12u3`, Make `4.3-4.1`, and
Ruff `0.15.15`. Ruff's exact version is owned by the imported SECS gate:
https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/secs/.github/workflows/ci.yml

Official package pages describe Git/Make:
https://packages.debian.org/bookworm/git
https://packages.debian.org/bookworm/make
The inventory was resolved on 2026-10-14 against authenticated Debian Bookworm,
Bookworm updates and Bookworm security amd64 indexes. Native apt simulated
`--no-install-recommends install git=1:2.39.5-0+deb12u3 make=4.3-4.1` against
the pinned base's unchanged dpkg status. It selected 18 new packages and the
required `perl-base` upgrade from `5.36.0-7+deb12u3` to `5.36.0-7+deb12u4`.
The declaration therefore contains 19 Debian artifacts and one Ruff wheel.
This is the complete addition set relative to the pinned base; inherited
dependencies remain supplied by that exact base, whose package inventory was
captured. Recommended and suggested packages are excluded.

Debian versions, filenames, sizes and SHA-256 values come from apt's authenticated
package stanzas. Security-pool artifacts use `/debian-security/pool/updates/main/`;
ordinary artifacts use `/debian/pool/main/`. Apt verified the signed catalogs
using the pinned base's `/usr/share/keyrings/debian-archive-keyring.gpg`, and a
separate offline gpgv pass verified all three retained InRelease files. Catalog
hashes, signature fingerprints and dates are recorded in
`checker-advisories.json`. The catalogue authorities are:
https://deb.debian.org/debian/dists/bookworm/InRelease
https://deb.debian.org/debian/dists/bookworm-updates/InRelease
https://deb.debian.org/debian-security/dists/bookworm-security/InRelease

Ruff's wheel is
`ruff-0.15.15-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`,
selected from official exact-release metadata:
https://pypi.org/pypi/ruff/0.15.15/json
Its SHA-256 is
`48decfa11d740de4889de623be1463308346312f2409a56e24aa280c86162dc4`.
The completed declaration's SHA-256 is
`dda2f9555dd64c637f6fe4c161ba0523e3da28dca08f51354c4230f50e0e0432`.
No package artifact was acquired or installed during resolution. The declaration
and advisory findings were independently reviewed before artifact acquisition.

`checker-advisories.json` records the exact-version advisory lookup at
2026-10-14T10:17:32Z, including every declared Debian binary's source package and
source version. Debian's source advisory records were compared with those exact
versions using dpkg version ordering; unresolved findings are preserved with
Debian's status, urgency and no-DSA rationale where present. The primary input is:
https://security-tracker.debian.org/tracker/data/json
Its captured SHA-256 is
`3c7d3ad804ac6d8c488f96919a8657590d36bac34dd8a46e9b40589ba3b9a58d`.

Source-level unresolved counts are Git 3, curl 38, expat 6, OpenLDAP 6,
Perl 5, nghttp2 1 and cyrus-sasl2 1. Git's three are classified unimportant;
other sources include issues with urgency not yet assigned. No unresolved entries
were returned for Make, brotli, liberror-perl, gdbm, libpsl, rtmpdump or libssh2;
Ruff's exact PyPI release metadata returned an empty vulnerabilities array.
Source entries can concern functionality absent from a selected binary; the
lookup conservatively retains them without claiming runtime exposure.
This is advisory checking, not a complete security audit or a claim that the
closure is vulnerability free. It does not audit all inherited base packages.
The checker runs offline under its runtime containment. The recorded findings
were accepted for that offline tool-only scope; network-library, remote Git,
XML and unused Perl triggers do not belong to its execution grant. Unresolved
and unassigned findings remain in the report. Direct-tool advisory pages:
https://security-tracker.debian.org/tracker/source-package/git
https://security-tracker.debian.org/tracker/source-package/make-dfsg
https://github.com/astral-sh/ruff/security

Local resolution evidence is under `.work/checker/metadata`: base image and worker
inspection, the base package inventory, signed catalogs and apt indexes, apt
commands and exits, exact package stanzas, captured primary metadata, gpgv status
and manifest validation. Bootstrap sources are retained under
`.work/qualification/checker-foundation-20261014`. Resolution used only the
launcher-owned proxy, the previously selected explicit interface and constrained
nonroot workers; both launcher-owned containers were retired. The base's apt
cleanup hook attempted to remove its read-only archive-cache glob and received
permission denied. Apt update still exited zero; no package action ran. A separate
offline pass sourced security-pool URLs from the authenticated stanzas and
validated the final declaration with the repository's closed-field checker.

The executed resolver snapshot is
`.work/qualification/checker-foundation-20261014/metadata_resolve.executed.py`,
SHA-256 `9a14155641348a5b1125ed33e3467ed2788366b2b97496e55cf0c52c9f91f3bb`.
The current resolver is a later correction with SHA-256
`00ad6a48c5e5a3a1f176244675fde122cb4c6ff679cb19e7bd9a75118efd2170`;
it is not claimed as the source of the original resolution run. The executed
offline finalizer has SHA-256
`f922cbc4b9b866a60782a42a4b30865a1a6cc547173acd2cd98a89e0b2b99424`
at the same directory's `metadata_finalize.py`. Its source hash, log and exit are
retained, but a standalone finalizer command/inspection capture was not retained;
those local files alone do not independently prove its container grants.

Magnet's separate dependency base and content identity establish this boundary:
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/containers/test/Dockerfile.base
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/Makefile#L387-L425

Offline builds capture the recipe, declaration, verifier and dependency bytes
before dispatch. Image publication requires the actual local immutable ID and
matching captured-input label. No Git database, engine socket, secrets or model
data enters this tool-only image. Ordinary checks receive their reviewed sources
from the runtime launcher rather than copying a whole checkout into the image.

The checker deliberately uses the selected local engine's built-in Dockerfile
frontend, matching the qualified inference/lock recipe boundary. It has no
external syntax directive: runtime network flags and `--pull=false` do not stop
an independent frontend-image fetch. The builder is explicitly `default`, with
ambient builder/config/backend selectors removed. Engine 29.8.1/API 1.56 was
observed locally; a real offline named-context/RUN-mount capability probe passed
before actual installation. The exact command, immutable probe image and installed
version checks are retained in
`.work/qualification/checker-foundation-20261014/materialization/REPORT.md`.
No external frontend image is provisioned.
