import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def pins(relative):
    return dict(re.findall(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)', (ROOT/relative).read_text(), re.M))


def test_runtime_and_test_python_locks_match_and_have_hashes():
    runtime = pins('apps/api/requirements.lock')
    tests = pins('apps/api/requirements-test.lock')
    assert runtime and runtime.items() <= tests.items()
    assert runtime['fastapi'] == '0.162.0' and runtime['starlette'] == '1.10.0'
    assert runtime['cryptography'] == '50.0.2'
    assert runtime['pyjwt'] == '2.15.1'
    assert runtime['zeep'] == '4.3.3'
    assert 'python-multipart' not in runtime and 'python-multipart' not in tests
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


def test_updated_crypto_reads_previous_camera_ciphertext(monkeypatch):
    from app.core import security

    # Public synthetic fixture generated with cryptography 44.0.2. Never a real
    # camera credential; the persisted Fernet format must survive an upgrade.
    monkeypatch.setattr(security.settings, 'encryption_key', 'stage14-public-compatibility-fixture-key')
    monkeypatch.setattr(security, 'fernet', security._build_fernet())
    previous = (
        'gAAAAABq64ltEtO51Pa9wDRHSXJuqIa97qVlTm86rFjb2RdiZS34vO0qjuJgWadqAdHGO_7UfmeW'
        'QI9-bMbZ_AV3w0sxqosvWa887ZB3CdbtEXoK-s0JiyI='
    )
    assert security.decrypt_text(previous) == 'stage14-fixture-camera-password'
    assert security.decrypt_text(security.encrypt_text('new-fixture')) == 'new-fixture'


def test_updated_jwt_reads_previous_access_token_without_weakening_verification(monkeypatch):
    import jwt
    from app.core import security

    monkeypatch.setattr(security.settings, 'jwt_secret', 'stage14-public-compatibility-fixture-jwt-key')
    # Public synthetic HS256 token produced with PyJWT 2.10.1 (expires 2099).
    previous = (
        'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
        'eyJ0eXAiOiJhY2Nlc3MiLCJzdWIiOiJzdGFnZTE0LWZpeHR1cmUiLCJ1aWQiOjE3LCJleHAiOjQwNzA5MDg4MDB9.'
        'hOVNsqQ_Mu_oU4RTnewTbKfVgOj6xA5XPc8gzuXslZI'
    )
    # Library compatibility is unchanged. Product credentials predating the
    # external restore generation now deliberately require one fresh login.
    assert jwt.decode(previous, security.settings.jwt_secret, algorithms=['HS256'])['uid'] == 17
    with pytest.raises(jwt.InvalidTokenError, match='user_session_expired'):
        security.decode_access_token(previous)
    assert security.decode_access_token(security.create_access_token('fixture', user_id=17))['uid'] == 17
    signed_data, signature = previous.rsplit('.', 1)
    with pytest.raises(jwt.InvalidSignatureError):
        security.decode_access_token(signed_data + '.A' + signature[1:])
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_access_token(jwt.encode({'typ': 'access', 'uid': 17}, '', algorithm='none'))
