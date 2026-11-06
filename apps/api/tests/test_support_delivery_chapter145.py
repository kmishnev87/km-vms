import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.core.config import settings
from app.routers import settings as routes
from app.services import support_delivery as delivery


def bundle():
    result=io.BytesIO()
    with zipfile.ZipFile(result,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("bug-report.txt","Safe description\n")
        z.writestr("system/manifest.json",'{"mode":"normal"}')
    result.seek(0)
    return result


class Reply:
    status_code=201
    payload=None
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def iter_content(self,size): yield json.dumps(self.payload).encode()


@pytest.mark.parametrize("url",["","http://example.org/reports","https://user:pass@example.org/reports","https://example.org/reports#frag","https://example.org/reports?token=x","https://example.org:bad/reports"])
def test_destination_is_server_configured_https_without_credentials_or_redirect_hints(monkeypatch,url):
    monkeypatch.setattr(settings,"support_report_url",url)
    with pytest.raises(HTTPException) as error: delivery.support_destination()
    assert error.value.detail["code"]=="support_not_configured"


def test_send_uses_redacted_zip_only_and_validates_matching_receipt(monkeypatch):
    rid=str(uuid4());seen={};response=Reply();response.payload={"accepted":True,"report_id":rid}
    class Client:
        trust_env=True
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self,url,**kwargs):
            seen.update(url=url,trust_env=self.trust_env,**kwargs)
            return response
    monkeypatch.setattr(delivery.requests,"Session",Client)
    archive=bundle()
    assert delivery.deliver_report(archive,mode="normal",submission_id=rid)=={"accepted":True,"report_id":rid}
    assert seen["trust_env"] is False and seen["allow_redirects"] is False and seen["stream"] is True
    assert int(seen["headers"]["Content-Length"])==archive.getbuffer().nbytes
    assert "Authorization" not in seen["headers"] and "Cookie" not in seen["headers"]
    response.payload={"accepted":True,"report_id":str(uuid4())}
    with pytest.raises(HTTPException):delivery.deliver_report(bundle(),mode="normal",submission_id=rid)


@pytest.mark.parametrize("status,code",[(302,"support_delivery_failed"),(429,"support_busy"),(507,"support_storage_full"),(500,"support_delivery_failed")])
def test_receiver_error_is_safe_and_typed(monkeypatch,status,code):
    response=Reply();response.status_code=status
    class Client:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):return response
    monkeypatch.setattr(delivery.requests,"Session",Client)
    with pytest.raises(HTTPException) as error:delivery.deliver_report(bundle(),mode="normal",submission_id=str(uuid4()))
    assert error.value.detail=={"code":code}


def test_explicit_consent_fields_and_modes_are_validated():
    payload={"text":"Problem","consent":True,"submission_id":str(uuid4())}
    assert routes.SupportReportRequest(**payload).mode=="normal"
    for changes in ({"consent":False},{"mode":"all"},{"destination":"http://127.0.0.1"}):
        with pytest.raises(ValidationError):routes.SupportReportRequest(**(payload|changes))
    with pytest.raises(ValidationError):routes.SupportReportRequest(text="Problem",submission_id=str(uuid4()))
    assert routes.BugReportRequest(text="Problem").mode=="normal"
    assert routes.BugReportRequest(text="Problem",mode="extended").mode=="extended"


def test_route_builds_complete_archive_then_sends_and_audits_receipt(monkeypatch):
    seen=[];rid=uuid4()
    monkeypatch.setattr(routes,"build_log_archive",lambda **kwargs: (seen.append(kwargs) or bundle()))
    monkeypatch.setattr(routes,"deliver_report",lambda archive,**kwargs:{"accepted":True,"report_id":kwargs["submission_id"]})
    monkeypatch.setattr(routes,"create_event",lambda **kwargs:seen.append(kwargs))
    req=Request({"type":"http","headers":[],"client":("127.0.0.1",80)})
    result=routes.send_support_report(routes.SupportReportRequest(text="Problem",mode="extended",consent=True,submission_id=rid),req,db=object(),current_user=SimpleNamespace(username="test-owner"))
    assert result["report_id"]==str(rid)
    assert seen[0]["include_logs"] is True and seen[0]["mode"]=="extended"
    assert seen[1]["event_type"]=="diagnostics.support_report_sent"
    assert "Problem" not in json.dumps(seen[1]["metadata"])


def test_failure_releases_gate_for_retry(monkeypatch):
    monkeypatch.setattr(routes,"build_log_archive",lambda **kwargs: (_ for _ in ()).throw(OSError("private path and secret")))
    req=Request({"type":"http","headers":[],"client":("127.0.0.1",80)})
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            routes.send_support_report(routes.SupportReportRequest(text="Problem",consent=True,submission_id=uuid4()),req,db=object(),current_user=SimpleNamespace(username="test-owner"))
        assert error.value.detail=={"code":"support_archive_failed"}


def test_local_status_never_probes_remote_service(monkeypatch):
    monkeypatch.setattr(delivery.requests,"Session",lambda: (_ for _ in ()).throw(AssertionError("No network probe allowed")))
    assert routes.support_status(current_user=None)["available"] is True
