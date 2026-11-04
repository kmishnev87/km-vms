from app.services.live_engine.process_log import BoundedProcessLog


def test_continuous_writer_keeps_descriptor_and_recent_messages(tmp_path):
    path = tmp_path / 'ffmpeg.log'
    log = BoundedProcessLog(limit=1024, retained=128)
    with path.open('ab', buffering=0) as ffmpeg:
        for index in range(100):
            marker = f'frame={index} speed=1x\n'.encode()
            ffmpeg.write(b'x' * 8192 + marker)
            tail, rotated = log.read_tail(path, 80)
            assert rotated
            assert marker.decode() in tail
            assert path.stat().st_size <= 128
            assert path.with_name('ffmpeg.log.1').stat().st_size <= 1024
        ffmpeg.write(b'frame=101\n')
        tail, rotated = log.read_tail(path, 80)
        assert not rotated
        assert tail.endswith('frame=101\n')
    assert len(list(tmp_path.iterdir())) == 2


def test_small_log_is_read_only(tmp_path):
    path = tmp_path / 'ffmpeg.log'
    path.write_bytes(b'frame=1\n')
    assert BoundedProcessLog().read_tail(path, 4000) == ('frame=1\n', False)
    assert not path.with_name('ffmpeg.log.1').exists()
