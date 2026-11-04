from types import SimpleNamespace

from app.services.onvif_service import summarize_main_sub_assignment


def camera(**changes):
    return SimpleNamespace(**dict(dict(rtsp_main_url='/main', rtsp_sub_url='/sub',
                                       onvif_profile_token='lens2-main',
                                       onvif_sub_profile_token='lens2-sub'), **changes))


def test_exact_sub_token_wins_over_ambiguous_paths_across_lenses():
    profiles = [dict(token=f'lens{lens}-{role}', stream_path=f'/{role}')
                for lens in (1, 2) for role in ('main', 'sub')]
    result = summarize_main_sub_assignment(camera(), profiles)
    assert result['main_confidence'] == result['sub_confidence'] == 'token_exact'
    assert result['status'] == 'ok'
    assert result['reason_codes'] == []


def test_old_camera_without_sub_token_can_use_unique_path():
    profiles = [dict(token='main', stream_path='/main'), dict(token='sub', stream_path='/sub')]
    result = summarize_main_sub_assignment(camera(onvif_profile_token=None, onvif_sub_profile_token=None), profiles)
    assert result['sub_confidence'] == 'path_unique'
    assert result['status'] == 'ok'


def test_missing_and_duplicate_tokens_remain_visible():
    profiles = [dict(token='lens2-main', stream_path='/main'), dict(token='lens1-sub', stream_path='/sub')]
    result = summarize_main_sub_assignment(camera(), profiles)
    assert 'sub_profile_token_not_seen' in result['reason_codes']
    assert result['status'] != 'ok'
    profiles += [dict(token='lens2-sub', stream_path='/a'), dict(token='lens2-sub', stream_path='/b')]
    result = summarize_main_sub_assignment(camera(), profiles)
    assert result['sub_confidence'] == 'token_ambiguous'
    assert result['status'] == 'warning'
