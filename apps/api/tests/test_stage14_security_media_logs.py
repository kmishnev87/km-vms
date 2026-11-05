"""Keep media failures useful without persisting temporary credentials."""
import ast
import logging
from pathlib import Path
import re

import pytest

from app.core.sanitization import redact_text

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("path", ["chronology/file", "chronology/download", "recordings/stream", "recordings/download", "live/1/main/index.m3u8", "live/2/sub2/seg_12.ts", "archive/exports/export-123/file"])
@pytest.mark.parametrize("slash", ["", "/"])
def test_all_media_routes_and_redirect_slashes_use_safe_diagnostics(path, slash):
    config = (ROOT / "deploy/nginx/default.conf").read_text()
    pattern = re.search(r"location ~ (.+) \{\n\s+access_log .*kmvms_media_error", config)[1]
    assert re.fullmatch(pattern.replace("(?<media_path>", "(?P<media_path>"), "/api/" + path + slash)
    log_format = config.split("log_format kmvms_media_error", 1)[1].split(";", 1)[0]
    assert "$uri" in log_format
    assert set(re.findall(r"\$([A-Za-z_]+)", log_format)) == {"request_method", "uri", "status", "upstream_status", "request_time"}
    media_block = config.split("if=$kmvms_log_media_error;", 1)[1].split("\n    }", 1)[0]
    assert "error_log /dev/null;" in media_block


@pytest.mark.parametrize("query", ["media_token=DUMMY-SECRET", "media%5Ftoken=DUMMY-SECRET", "media_token=DUMMY-SECRET&media_token=DUMMY-SECOND"])
def test_actual_uvicorn_filter_masks_query_in_record_arguments(query):
    tree = ast.parse((ROOT / "apps/api/app/main.py").read_text())
    declaration = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AccessLogRedactionFilter")
    namespace = {"logging": logging, "redact_text": redact_text}
    exec(compile(ast.Module(body=[declaration], type_ignores=[]), "actual-access-log-filter", "exec"), namespace)
    record = logging.LogRecord("uvicorn.access", logging.INFO, "fixture", 1, '%s - "%s %s HTTP/%s" %d', ("test-client", "GET", "/api/chronology/file/?" + query, "1.1", 307), None)
    assert namespace["AccessLogRedactionFilter"]().filter(record)
    assert "DUMMY-SECRET" not in record.getMessage() and "DUMMY-SECOND" not in record.getMessage()
