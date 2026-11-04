import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def pins(relative):
    return dict(re.findall(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)', (ROOT/relative).read_text(), re.M))


def test_runtime_and_test_python_locks_match_and_have_hashes():
    runtime = pins('apps/api/requirements.lock')
    tests = pins('apps/api/requirements-test.lock')
    assert runtime and runtime.items() <= tests.items()
    assert runtime['fastapi'] == '0.162.0' and runtime['starlette'] == '1.10.0'
    for relative in ['apps/api/requirements.lock','apps/api/requirements-test.lock','apps/recorder/requirements.lock']:
        content = (ROOT/relative).read_text()
        packages = pins(relative)
        assert packages
        assert content.count('--hash=sha256:') >= len(packages)
        assert not re.search(r'^-e|^git\+|^--index-url|^--trusted-host', content, re.M)


def test_docker_builds_use_locked_dependencies_and_digest_bases():
    for relative in ['apps/api/Dockerfile','apps/api/Dockerfile.test','apps/recorder/Dockerfile',
                     'apps/web/Dockerfile','apps/update-helper/Dockerfile','apps/update-control-plane/Dockerfile']:
        content = (ROOT/relative).read_text()
        assert re.search(r'^FROM \S+@sha256:[a-f0-9]{64}$', content, re.M), relative
        if 'pip install' in content:
            assert '--require-hashes' in content and '.lock' in content
    web = (ROOT/'apps/web/Dockerfile').read_text()
    assert 'node:24.' in web and 'npm ci' in web and 'RUN npm install' not in web


def test_install_and_nested_setup_helpers_do_not_reintroduce_floating_images():
    floating = re.compile(r'(?:python:3\.12-slim|docker:27-cli)(?!@sha256:[a-f0-9]{64})')
    for path in (ROOT / 'scripts').rglob('*.sh'):
        assert not floating.search(path.read_text()), path.relative_to(ROOT)
