# KM VMS Backend Pytest Runner

This repository provides a NAS-contained backend pytest runner.

Use it from the repository root on NAS:

```sh
sh scripts/run_backend_tests.sh
```

Target one backend test file:

```sh
sh scripts/run_backend_tests.sh apps/api/tests/test_storage_recording_contract.py
```

Target one test function:

```sh
sh scripts/run_backend_tests.sh apps/api/tests/test_system_runtime_status_contract.py::test_runtime_status_shape
```

Pass extra pytest args normally:

```sh
sh scripts/run_backend_tests.sh apps/api/tests -k storage -x
```

The runner uses:

- `docker-compose.pytest.yml`
- `apps/api/Dockerfile.test`
- `apps/api/requirements-test.txt`
- `scripts/run_backend_tests.sh`

Do not install pytest globally on the NAS host. Do not install pytest manually into a running production API container. Use this runner for targeted and full backend pytest.
