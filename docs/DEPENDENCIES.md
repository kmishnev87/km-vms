# Dependency builds

The input manifests are `apps/api/requirements.txt`,
`apps/api/requirements-test.txt`, `apps/recorder/requirements.txt` and
`apps/web/package.json`. Runtime builds install from checked-in lock files;
do not edit a generated lock by hand or regenerate it during installation.

Python locks target CPython 3.12 on Linux (the product container platform).
Generate them in a disposable container using `pip-tools==7.6.2`:

```
cd apps/api
pip-compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url --no-emit-trusted-host -o requirements.lock requirements.txt
pip-compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url --no-emit-trusted-host -c requirements.lock -o requirements-test.lock requirements-test.txt
cd ../recorder
pip-compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url --no-emit-trusted-host -o requirements.lock requirements.txt
```

API, test and Recorder images install with `pip --require-hashes`. Test locks
use the runtime lock as a constraint. Hashes cover the upstream release files,
not just the build host's wheel. Updating a version requires intentional lock
regeneration and the relevant product regressions. No resolver is a runtime
service and no lock generator is installed in an active product container.

Web builds use `npm ci` with `package-lock.json`. Regenerate the lock on the
pinned Node 24 LTS build platform, review `npm audit --omit=dev`, build, and check
browser/media behavior. An audit label alone is not proof that a condition is
reachable in this application. React 18 and hls.js are intentionally unchanged.

Dockerfiles and external Compose images use multi-platform manifest digests.
Except for the agreed Node LTS upgrade, these pin the previously tested base
images rather than silently replacing operating systems or database majors.
Update tags and digests together after validation; never relabel an old image
as a newly built product release.

Scope: application dependencies and base-image identity are fixed. This is
not a bit-for-bit offline build guarantee: apt/apk package repositories and
the Jellyfin repository remain external package sources. Image content and
installed FFmpeg capabilities still need release validation.
