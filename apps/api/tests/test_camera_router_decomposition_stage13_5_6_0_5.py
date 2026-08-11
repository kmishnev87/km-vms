import ast
import sys
from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.main import app
import app.routers.camera_connection_helpers as connection_helpers
import app.routers.camera_onvif_routes as onvif_routes
import app.routers.cameras as cameras


EXPECTED_ONVIF_ROUTES = {
    ("POST", "/cameras/onvif/profiles", "onvif_profiles"),
    ("POST", "/cameras/onvif/discover", "onvif_discover"),
    ("POST", "/cameras/onvif/probe", "onvif_probe"),
    ("POST", "/cameras/onvif/profile_config", "onvif_profile_config"),
    ("POST", "/cameras/onvif/update_profile", "update_onvif_profile_route"),
    ("GET", "/cameras/{camera_id}/onvif/ptz/capabilities", "onvif_ptz_capabilities"),
    ("POST", "/cameras/{camera_id}/onvif/ptz/command", "onvif_ptz_command"),
    ("GET", "/cameras/{camera_id}/onvif/health", "onvif_health"),
    ("POST", "/cameras/{camera_id}/onvif/health/check", "onvif_health_check"),
}

CONNECTION_FACADE_SYMBOLS = {
    "PROOF_TTL_SECONDS",
    "ONVIF_PROBE_PROOFS",
    "RTSP_TEST_PROOFS",
    "safe_onvif_error",
    "onvif_error_code",
    "validation_fingerprint",
    "register_validation_proof",
    "register_onvif_probe_proof",
    "register_rtsp_test_proof",
    "store_has_valid_proof",
    "has_valid_onboarding_proof",
    "require_save_gate",
    "safe_int",
    "parse_bounded_int",
    "parse_port",
    "assemble_rtsp_url",
    "get_camera_credentials",
    "saved_stream_path",
    "profile_matches_stream",
    "apply_profile_assignments",
    "build_test_url",
    "safe_preview_token",
}

ONVIF_FACADE_SYMBOLS = {
    "get_active_camera_or_404",
    "safe_camera_onvif_credentials",
    "run_bounded_read_only_check",
    "onvif_profiles",
    "onvif_discover",
    "onvif_probe",
    "onvif_profile_config",
    "update_onvif_profile_route",
    "onvif_ptz_capabilities",
    "onvif_ptz_command",
    "onvif_health",
    "onvif_health_check",
}


def test_onvif_routes_are_included_once_and_owned_by_extracted_router():
    actual = []
    endpoint_modules = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            key = (method, route.path, route.name)
            if key in EXPECTED_ONVIF_ROUTES:
                actual.append(key)
                endpoint_modules[key] = route.endpoint.__module__

    assert Counter(actual) == Counter(EXPECTED_ONVIF_ROUTES)
    assert set(endpoint_modules.values()) == {"app.routers.camera_onvif_routes"}


def test_onvif_route_permissions_remain_manage_cameras():
    decisions = {(item.method, item.path): item.decision for item in ENDPOINT_PERMISSIONS}
    for method, path, _name in EXPECTED_ONVIF_ROUTES:
        assert decisions[(method, path)] == "manage_cameras"


def test_cameras_module_is_a_compatibility_facade_for_extracted_symbols():
    for name in CONNECTION_FACADE_SYMBOLS:
        assert getattr(cameras, name) is getattr(connection_helpers, name)
    for name in ONVIF_FACADE_SYMBOLS:
        assert getattr(cameras, name) is getattr(onvif_routes, name)

    assert cameras.ONVIF_PROBE_PROOFS is connection_helpers.ONVIF_PROBE_PROOFS
    assert cameras.RTSP_TEST_PROOFS is connection_helpers.RTSP_TEST_PROOFS


def test_extracted_modules_do_not_import_the_cameras_facade():
    router_dir = Path(cameras.__file__).resolve().parent
    for filename in ("camera_connection_helpers.py", "camera_onvif_routes.py"):
        source = (router_dir / filename).read_text(encoding="utf-8")
        assert "app.routers.cameras" not in source


def test_moved_implementations_have_one_owner_and_safe_token_contract_is_stable():
    source = Path(cameras.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    facade_definitions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not facade_definitions.intersection(CONNECTION_FACADE_SYMBOLS | ONVIF_FACADE_SYMBOLS)
    assert source.count("router.include_router(onvif_router)") == 1

    assert connection_helpers.safe_preview_token(None) is None
    assert connection_helpers.safe_preview_token("") is None
    assert connection_helpers.safe_preview_token("  ") is None
    assert connection_helpers.safe_preview_token("abc-DEF_123") == "abc-DEF_123"
    assert connection_helpers.safe_preview_token("unsafe/token") is None
