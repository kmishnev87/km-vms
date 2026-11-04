import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from test_emergency_restart_continuity_v5 import bootstrap, REPO_ROOT


def source_copy(path):
    scripts = path / 'scripts'
    for name in bootstrap.SOURCE_FILES:
        target = scripts / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / 'scripts' / name, target)
    return path


def load_slots(script, name):
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_slot_components_follow_exact_source_not_another_release_on_sys_path(tmp_path, monkeypatch):
    first = source_copy(tmp_path / 'first')
    second = source_copy(tmp_path / 'second')
    contract = first / 'scripts/km_vms_slot_core/contracts.py'
    value = contract.read_text()
    assert 'MAX_INVENTORY_ENTRIES = 200_000' in value
    contract.write_text(value.replace('MAX_INVENTORY_ENTRIES = 200_000', 'MAX_INVENTORY_ENTRIES = 12345'))
    monkeypatch.syspath_prepend(str(second / 'scripts'))
    own = load_slots(first / 'scripts/km-vms-release-slots.py', 'stage14_first_slots')
    other = load_slots(second / 'scripts/km-vms-release-slots.py', 'stage14_second_slots')
    assert own.MAX_INVENTORY_ENTRIES == 12345
    assert other.MAX_INVENTORY_ENTRIES == 200_000
    assert own.SlotStateStore.__module__ != other.SlotStateStore.__module__
    assert Path(own._slot_components.__file__).parent == first / 'scripts/km_vms_slot_core'
    assert not list(first.rglob('__pycache__'))


def test_bootstrap_hashes_nested_components_and_rejects_parent_symlink(tmp_path):
    source = source_copy(tmp_path / 'source')
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'data').mkdir()
    (app / '.env').write_text('COMPOSE_PROJECT_NAME=fixture\n')
    installed = bootstrap.install_bundle(app, source)
    bundle = Path(installed['bundle_path'])
    engine = bootstrap.load_slot_engine(bundle)
    assert Path(engine._slot_components.__file__).parent == bundle / 'km_vms_slot_core'
    assert not list(bundle.rglob('__pycache__'))
    package = bundle / 'km_vms_slot_core'
    original = bundle / 'original-components'
    package.rename(original)
    package.symlink_to(original, target_is_directory=True)
    with pytest.raises(bootstrap.BootstrapError, match='directory is unsafe'):
        bootstrap.validate_current_bundle(app)
    package.unlink()
    original.rename(package)
    bootstrap.validate_current_bundle(app)
