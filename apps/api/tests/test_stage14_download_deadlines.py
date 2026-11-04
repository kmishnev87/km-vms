import os
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("script", ["install.sh", "update.sh"])
def test_download_watchdog_terminates_stuck_child_and_allows_next_attempt(tmp_path, script):
    source=(ROOT/"scripts"/script).read_text()
    monitor="stop_download() {"+source.split("stop_download() {",1)[1].split("\nhttp_download() {",1)[0]
    clock=tmp_path/"clock"
    harness=tmp_path/"watchdog.sh"
    harness.write_text(monitor+f'''
date() {{ v=0; [ ! -f "{clock}" ] || v=$(cat "{clock}"); v=$((v+61)); printf '%s' "$v" > "{clock}"; printf '%s\\n' "$v"; }}
sleep 60 &
owned_pid=$!
status=0
wait_download "$owned_pid" "{tmp_path}/partial" "" 900 || status=$?
[ "$status" = 28 ] || exit 21
! kill -0 "$owned_pid" 2>/dev/null || exit 22
[ -z "$DOWNLOAD_PID" ] || exit 23
# A later normal transfer remains usable.
(printf 'data' > "{tmp_path}/complete") &
normal_pid=$!
wait "$normal_pid"
''')
    result=subprocess.run(["sh",str(harness)],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr
    assert (tmp_path/"complete").read_text()=="data"


@pytest.mark.parametrize("script", ["install.sh", "update.sh"])
def test_transport_paths_have_connection_idle_and_total_limits(script):
    source=(ROOT/"scripts"/script).read_text()
    subprocess.run(["sh","-n",str(ROOT/"scripts"/script)],check=True)
    assert "--connect-timeout 15" in source
    assert "--speed-limit 1 --speed-time 60" in source
    assert "--max-time 30" in source
    assert 'wait_download "$!"' in source
    assert "trap 'exit 143' TERM" in source
