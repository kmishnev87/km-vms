from types import SimpleNamespace

import pytest

from app.services.live_engine.pipeline_policy import PipelinePolicy


@pytest.mark.parametrize('codec,backend,available,expected,selected', [
    ('h264', 'auto', [], 'copy', 'copy'),
    ('hevc', 'auto', [], 'fallback_transcode', 'cpu'),
    ('h264', 'cpu', [], 'fallback_transcode', 'cpu'),
    ('hevc', 'qsv', ['qsv'], 'hardware_transcode', 'qsv'),
    ('h264', 'qsv', ['qsv'], 'hardware_transcode', 'qsv'),
    ('hevc', 'qsv', [], 'fallback_transcode', 'cpu'),
])
def test_pipeline_selection_without_process_or_viewer_side_effects(codec, backend, available, expected, selected):
    failed = {}
    attempted = []
    config = SimpleNamespace(live_hwaccel_mode='auto', live_hwaccel_device=None,
                             live_video_codec='auto', live_transcode=False)
    policy = PipelinePolicy(stream='sub', failed_backends=failed, attempted_backends=attempted,
                            last_fallback_reason=None, decision_reason=None, force_stable_fps=False,
                            high_cpu_risk=False, audio_mode='auto', config=config)
    probe = SimpleNamespace(error=None, codec=codec, width=1280, height=720, fps=15,
                            audio_codec='aac', audio_channels=1, audio_sample_rate=16000,
                            audio_error=None, safe_for_copy=codec == 'h264')
    caps = {'config': {'backend': backend}, 'available_backends': available,
            'backend_priority': available, 'hardware_accel_available': bool(available)}
    mode = policy._choose_mode(SimpleNamespace(id=1), probe, caps)
    changes = policy.changes()
    assert (mode, changes['selected_backend']) == (expected, selected)
    assert changes['input_codec'] == codec
    assert changes['audio_reason'] == 'input_has_audio'
    assert failed == {} and attempted == []
    assert not hasattr(policy, 'proc') and not hasattr(policy, 'viewers')
