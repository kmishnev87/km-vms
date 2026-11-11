// A source change aborts pending play() calls. It is not an autoplay rejection
// and must not erase the user's listening choice for the next stream.
export function playLiveAudio(video, onBlocked) {
  let cancelled = false;
  const failed = error => {
    if (cancelled || error?.name !== "NotAllowedError") return;
    onBlocked?.(error);
  };
  try {
    video.play()?.catch?.(failed);
  } catch (error) {
    failed(error);
  }
  return () => { cancelled = true; };
}

export function liveAudioAllowed(item) {
  const mode = String(item?.audio_mode || "unknown").toLowerCase();
  return item?.audio_enabled !== false && !["none", "off", "disable", "disabled"].includes(mode);
}

// A muxed HLS stream need not list a separate audio rendition in its playlist.
// The audio buffer codec is positive evidence from the actual media, even when
// an earlier RTSP probe failed. An explicit configuration ban still wins.
export function confirmedHlsAudio(tracks, allowed = true) {
  const track = tracks?.audio || tracks?.audiovideo;
  if (!allowed || !track || typeof track !== "object") return null;
  const codec = String(track.codec || track.levelCodec || "").trim();
  if (!codec) return null;
  return { audio_mode: "hls", audio_enabled: true, audio_available: true,
    audio_reason: "input_has_audio", input_audio_codec: tracks.audio ? codec : "" };
}
