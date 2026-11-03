from pathlib import Path

from test_archive_export_stage11 import (
    api_db, client, add_user, add_camera, add_media_segment, create_done_export,
    auth_headers, ROLE_ADMIN, ROLE_VIEWER, export_service, settings,
)
from app.services.media_tokens import create_media_token


def ready_clip(client, api_db):
    db, _root = api_db
    actor = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    source = add_media_segment(db, camera, seconds=2)
    job = create_done_export(client, db, actor, camera, source)
    assert client.post(f'/archive/exports/{job.id}/manifest', headers=auth_headers(actor)).status_code == 200
    return db, actor, job


def test_native_download_is_scoped_streamed_reusable_and_hashes_once(client, api_db, monkeypatch):
    db, actor, job = ready_clip(client, api_db)
    endpoint = f'/archive/exports/{job.id}'
    count = []
    original = export_service._sha256
    monkeypatch.setattr(export_service, '_sha256', lambda path: (count.append(path), original(path))[1])
    issued = client.post(endpoint + '/download-token', headers=auth_headers(actor))
    assert issued.status_code == 200
    assert count == []
    info = issued.json()
    assert info['filename'].endswith('.mkv') and info['size_bytes'] > 0
    assert client.get(endpoint + '/file').status_code == 401
    file = client.get(endpoint + '/file', params={'media_token': info['media_token']})
    assert file.status_code == 200 and 'attachment' in file.headers['content-disposition']
    assert file.headers['cache-control'] == 'private, no-store'
    assert len(count) == 1
    assert file.content == (Path(settings.storage_exports) / job.internal_output_path).read_bytes()
    repeated = client.get(endpoint + '/file', params={'media_token': info['media_token']})
    assert repeated.content == file.content and len(count) == 2
    metadata = client.get(endpoint, headers=auth_headers(actor)).json()
    assert metadata['filename'] == info['filename']
    assert metadata['download_ready'] is True


def test_native_download_rechecks_user_scope_resource_and_checksum(client, api_db):
    db, actor, job = ready_clip(client, api_db)
    endpoint = f'/archive/exports/{job.id}'
    viewer = add_user(db, role=ROLE_VIEWER, username='native-viewer')
    assert client.post(endpoint + '/download-token', headers=auth_headers(viewer)).status_code == 403
    assert client.post(endpoint + '/download-token').status_code == 401
    wrong, _ = create_media_token(user=actor, scope='archive-export-download', resource={'export_id': 'different', 'checksum': job.internal_checksum, 'action': 'download'})
    assert client.get(endpoint + '/file', params={'media_token': wrong}).status_code == 403
    wrong_scope, _ = create_media_token(user=actor, scope='recording', resource={'export_id': job.id, 'checksum': job.internal_checksum, 'action': 'download'})
    assert client.get(endpoint + '/file', params={'media_token': wrong_scope}).status_code == 403
    info = client.post(endpoint + '/download-token', headers=auth_headers(actor)).json()
    actor.is_active = False
    db.commit()
    assert client.get(endpoint + '/file', params={'media_token': info['media_token']}).status_code == 401
    actor.is_active = True
    db.commit()
    path = Path(settings.storage_exports) / job.internal_output_path
    path.write_bytes(path.read_bytes() + b'corrupt')
    assert client.get(endpoint + '/file', params={'media_token': info['media_token']}).status_code == 409


def test_interrupted_generation_can_resume_without_creating_another_job(client, api_db):
    db, actor, job = ready_clip(client, api_db)
    job.status = 'running'  # persisted status after a process exit; no live lease
    db.commit()
    owned_stale = Path(settings.storage_exports) / export_service.EXPORT_TEMP_DIR / f'stage11_stage2_{job.id}_interrupted'
    unrelated = owned_stale.parent / 'other-job-temp'
    owned_stale.mkdir(parents=True)
    (owned_stale / 'part.mkv').write_bytes(b'incomplete')
    unrelated.mkdir()
    (unrelated / 'keep').write_bytes(b'preserve')
    metadata = client.get(f'/archive/exports/{job.id}', headers=auth_headers(actor)).json()
    assert metadata['can_resume'] is True
    response = client.post(f'/archive/exports/{job.id}/generate?resume=true', headers=auth_headers(actor))
    assert response.status_code == 200 and response.json()['status'] == 'done'
    assert response.json()['id'] == job.id
    assert not owned_stale.exists() and (unrelated / 'keep').exists()


def test_active_generation_is_never_resumed_in_parallel(client, api_db):
    db, actor, job = ready_clip(client, api_db)
    job.status = 'running'
    db.commit()
    lock = export_service._generation_lock(job.id)
    lock.acquire()
    try:
        metadata = client.get(f'/archive/exports/{job.id}', headers=auth_headers(actor)).json()
        assert metadata['can_resume'] is False
        assert client.post(f'/archive/exports/{job.id}/generate?resume=true', headers=auth_headers(actor)).status_code == 409
    finally:
        lock.release()


def test_exports_have_no_unauthenticated_static_nginx_alias():
    config = (Path(__file__).resolve().parents[3] / 'deploy/nginx/default.conf').read_text()
    location = config.split('location /exports/ {', 1)[1].split('}', 1)[0]
    assert 'return 404;' in location and 'alias' not in location
