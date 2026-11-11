"""Bounded fMP4 framing for the Live Engine's on-demand MSE transport."""
from __future__ import annotations

MAX_BOX_BYTES = 16 * 1024 * 1024


def mp4_box_size(header: bytes) -> tuple[int, int]:
    if len(header) < 8:
        raise ValueError("mp4_header_incomplete")
    size = int.from_bytes(header[:4], "big")
    length = 16 if size == 1 else 8
    if size == 1:
        if len(header) < 16:
            raise ValueError("mp4_header_incomplete")
        size = int.from_bytes(header[8:16], "big")
    if size < length or size > MAX_BOX_BYTES:
        raise ValueError("mp4_box_out_of_bounds")
    return size, length


async def read_box(reader) -> tuple[bytes, bytes]:
    header = await reader.readexactly(8)
    if int.from_bytes(header[:4], "big") == 1:
        header += await reader.readexactly(8)
    size, length = mp4_box_size(header)
    return header[4:8], header + await reader.readexactly(size - length)


def _configuration(data: bytes, kind: bytes) -> bytes | None:
    at = data.find(kind)
    if at < 4:
        return None
    size = int.from_bytes(data[at - 4:at], "big")
    if not 8 < size <= len(data) - at + 4:
        return None
    return data[at + 4:at - 4 + size]


def initialization_info(moov: bytes) -> dict:
    avc = _configuration(moov, b"avcC")
    hevc = _configuration(moov, b"hvcC")
    if avc and len(avc) >= 4 and avc[0] == 1:
        codec = "avc1." + avc[1:4].hex().upper()
        input_codec = "h264"
    elif hevc and len(hevc) >= 13 and hevc[0] == 1:
        prefix = "hvc1" if b"hvc1" in moov else "hev1"
        space = ("", "A", "B", "C")[hevc[1] >> 6]
        profile = hevc[1] & 31
        compatibility = int(f"{int.from_bytes(hevc[2:6], 'big'):032b}"[::-1], 2)
        tier = "H" if hevc[1] & 32 else "L"
        constraints = ".".join(f"{item:02X}" for item in hevc[6:12].rstrip(b"\0"))
        codec = f"{prefix}.{space}{profile}.{compatibility:X}.{tier}{hevc[12]}"
        if constraints:
            codec += "." + constraints
        input_codec = "hevc"
    else:
        raise ValueError("mse_codec_unsupported")
    audio = b"mp4a" in moov
    codecs = codec + (",mp4a.40.2" if audio else "")
    return {"type": "init", "mime": f'video/mp4; codecs="{codecs}"',
            "input_codec": input_codec, "audio_available": audio}


def build_mse_command(camera, stream: str, input_url: str, audio_mode: str) -> list[str]:
    # Copy the source video: neither a GPU nor a software video encoder is needed.
    # Fragment duration is independent of GOP length. Only joining/recovery waits
    # for a source keyframe, not every delivered fragment.
    audio = ["-an"] if audio_mode in {"none", "off", "disable", "disabled"} else [
        "-map", "0:a:0?", "-c:a", "aac", "-profile:a", "aac_low", "-ar", "44100", "-ac", "1",
    ]
    return ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-rtsp_transport", (camera.rtsp_transport or "tcp").lower(), "-timeout", "5000000",
            "-fflags", "+genpts+nobuffer", "-analyzeduration", "1000000", "-probesize", "1000000",
            "-i", input_url, "-map", "0:v:0", "-c:v", "copy", *audio,
            "-avoid_negative_ts", "make_zero", "-movflags",
            "empty_moov+default_base_moof+frag_keyframe+skip_trailer+negative_cts_offsets",
            "-frag_duration", "100000", "-flush_packets", "1", "-f", "mp4", "pipe:1"]
