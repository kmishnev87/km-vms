import importlib.metadata

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient


def test_compatible_patched_framework_is_installed():
    assert importlib.metadata.version('fastapi') == '0.162.0'
    assert importlib.metadata.version('starlette') == '1.10.0'


def test_file_response_preserves_video_range_seek_head_and_download(tmp_path):
    path = tmp_path / 'video.bin'
    data = bytes(range(256)) * 16
    path.write_bytes(data)
    app = FastAPI()

    @app.api_route('/video', methods=['GET', 'HEAD'])
    def video():
        return FileResponse(path, filename='archive.bin', media_type='application/octet-stream')

    with TestClient(app) as client:
        response = client.get('/video')
        assert response.status_code == 200 and response.content == data
        assert 'archive.bin' in response.headers['content-disposition']
        response = client.get('/video', headers={'Range':'bytes=10-19'})
        assert response.status_code == 206 and response.content == data[10:20]
        assert response.headers['content-range'] == 'bytes 10-19/4096'
        response = client.get('/video', headers={'Range':'bytes=-8'})
        assert response.status_code == 206 and response.content == data[-8:]
        response = client.get('/video', headers={'Range':'bytes=1-3,8-9'})
        assert response.status_code == 206
        assert 'multipart/byteranges' in response.headers['content-type']
        assert data[1:4] in response.content and data[8:10] in response.content
        assert client.get('/video', headers={'Range':'bytes=99999-'}).status_code == 416
        response = client.head('/video')
        assert response.status_code == 200 and not response.content


def test_nested_router_inventory_matches_real_dispatch_and_keeps_access_dependency():
    app = FastAPI()
    child = APIRouter(prefix='/camera')

    def deny():
        raise HTTPException(status_code=403, detail='forbidden')

    @child.get('/protected', dependencies=[Depends(deny)])
    def protected():
        raise AssertionError('permission dependency must run before the endpoint')

    parent = APIRouter(prefix='/v1')
    parent.include_router(child)
    app.include_router(parent, prefix='/api')
    routes = [route for route in iter_route_contexts(app.routes)
              if isinstance(route.original_route, APIRoute)]
    assert [(route.path, route.name, route.methods) for route in routes] == [
        ('/api/v1/camera/protected', 'protected', {'GET'})
    ]
    assert routes[0].endpoint is protected
    with TestClient(app) as client:
        assert client.get(routes[0].path).status_code == 403
