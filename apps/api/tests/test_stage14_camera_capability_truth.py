from types import SimpleNamespace

import pytest
from app.services import onvif_service as onvif


@pytest.mark.parametrize('text', ['PTZ service timed out', 'WSDL read error', 'media service connection refused'])
def test_transport_or_local_wsdl_failure_is_not_unsupported(monkeypatch, text):
    def failure(*args, **kwargs):
        raise RuntimeError(text)
    monkeypatch.setattr(onvif, '_prepare_ptz_context', failure)
    with pytest.raises(RuntimeError):
        onvif.get_onvif_ptz_capabilities('example', 80, 'user', 'pass')


def test_node_read_failure_remains_unknown(monkeypatch):
    class PTZ:
        def GetNodes(self):
            raise TimeoutError('network')
    monkeypatch.setattr(onvif, '_prepare_ptz_context', lambda *args: (None, PTZ(), 'profile'))
    caps = onvif.get_onvif_ptz_capabilities('example', 80, 'user', 'pass')
    assert caps['supported'] is None
    assert onvif.normalize_ptz_compatibility(caps)['status'] == 'unknown'
    assert onvif.normalize_ptz_compatibility(dict(source='unsupported', supported=False))['status'] == 'unsupported'


@pytest.mark.parametrize('fail', [True, False])
def test_events_distinguish_failed_discovery_and_confirmed_absence(monkeypatch, fail):
    class Device:
        def GetServices(self, request):
            if fail:
                raise TimeoutError('network')
            return []
        def GetCapabilities(self, request):
            if fail:
                raise TimeoutError('network')
            return SimpleNamespace(Events=None)
    monkeypatch.setattr(onvif, 'ONVIFCamera', lambda *args: SimpleNamespace(devicemgmt=Device()))
    result = onvif.check_onvif_events_feasibility('example', 80, 'user', 'pass')
    assert result['events_status'] == ('unknown' if fail else 'unsupported')
    assert result['events_supported'] is (None if fail else False)
