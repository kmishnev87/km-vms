FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Yekaterinburg

WORKDIR /app

RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates \
      postgresql-client \
      curl \
      gnupg \
      tzdata \
      libdrm2 \
      libva2 \
      libva-drm2 \
      vainfo \
      intel-media-va-driver \
      mesa-va-drivers; \
    for package in libvpl2 libmfx1; do \
      if apt-cache show "$package" >/dev/null 2>&1; then \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$package"; \
      else \
        echo "Skipping unavailable optional media package: $package"; \
      fi; \
    done; \
    groupadd -f render; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    mkdir -p /etc/apt/keyrings; \
    echo "Installing Jellyfin FFmpeg with bounded network retries"; \
    jellyfin_key=/tmp/jellyfin_team.gpg.key; \
    jellyfin_curl_opts="-fsSL -4 --http1.1 --connect-timeout 15 --max-time 90 --retry 4 --retry-delay 5"; \
    if curl --help all 2>/dev/null | grep -q -- '--retry-all-errors'; then \
      jellyfin_curl_opts="$jellyfin_curl_opts --retry-all-errors"; \
    fi; \
    curl $jellyfin_curl_opts https://repo.jellyfin.org/jellyfin_team.gpg.key -o "$jellyfin_key" \
      || { echo "Jellyfin FFmpeg repository key download failed within bounded timeout/retry limits."; exit 1; }; \
    [ -s "$jellyfin_key" ] || { echo "Jellyfin FFmpeg repository key download returned an empty file."; exit 1; }; \
    gpg --dearmor --yes -o /etc/apt/keyrings/jellyfin.gpg "$jellyfin_key"; \
    rm -f "$jellyfin_key"; \
    . /etc/os-release; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/jellyfin.gpg] https://repo.jellyfin.org/debian ${VERSION_CODENAME} main" \
      > /etc/apt/sources.list.d/jellyfin.list; \
    apt_opts="-o Acquire::ForceIPv4=true -o Acquire::Retries=4 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"; \
    apt-get $apt_opts update \
      || { echo "Jellyfin FFmpeg repository apt update failed within bounded timeout/retry limits."; exit 1; }; \
    DEBIAN_FRONTEND=noninteractive apt-get $apt_opts install -y --no-install-recommends jellyfin-ffmpeg7 \
      || { echo "Jellyfin FFmpeg package install failed within bounded timeout/retry limits."; exit 1; }; \
    ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg; \
    ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe; \
    ffmpeg -hide_banner -version; \
    ffmpeg -hide_banner -hwaccels; \
    rm -f "$jellyfin_key"; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
