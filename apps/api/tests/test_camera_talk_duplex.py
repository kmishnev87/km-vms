from types import SimpleNamespace

from app.services.camera_talk.duplex import profile_duplex


def test_audio_options_keep_unknown_separate_from_half_and_full():
    profile = SimpleNamespace(AudioOutputConfiguration=SimpleNamespace(OutputToken="speaker"),
                              AudioSourceConfiguration=object(), AudioEncoderConfiguration=object(),
                              AudioDecoderConfiguration=object())
    assert profile_duplex(profile) == "unknown"
    options = SimpleNamespace(OutputTokensAvailable=["speaker"], SendPrimacyOptions=[])
    assert profile_duplex(profile, options) == "full"
    options.SendPrimacyOptions = ["http://www.onvif.org/ver20/HalfDuplex/Auto"]
    assert profile_duplex(profile, options) == "push_to_talk"
    profile.AudioOutputConfiguration.SendPrimacy = "http://www.onvif.org/ver20/HalfDuplex/Client"
    assert profile_duplex(profile) == "push_to_talk"
