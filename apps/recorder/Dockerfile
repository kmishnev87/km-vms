FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Yekaterinburg

WORKDIR /app

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    procps \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

COPY requirements-ai.lock /tmp/requirements-ai.lock
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements-ai.lock
COPY ai-model/intel-opencl.lock /tmp/km-vms-intel-opencl.lock
RUN if [ "$(dpkg --print-architecture)" = amd64 ]; then \
      apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ocl-icd-libopencl1 libnuma1 libdrm2 && rm -rf /var/lib/apt/lists/* && \
      install -d /opt/kmvms-intel && \
      while read -r digest file url; do \
        curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 --max-time 150 "$url" -o "/tmp/$file" && \
        printf '%s  %s\n' "$digest" "/tmp/$file" | sha256sum -c - && \
        dpkg-deb -x "/tmp/$file" /opt/kmvms-intel && rm -f -- "/tmp/$file" || exit 1; \
      done < /tmp/km-vms-intel-opencl.lock; \
    fi
RUN if [ -f /opt/kmvms-intel/etc/OpenCL/vendors/intel.icd ]; then \
      printf '%s\n' /opt/kmvms-intel/usr/lib/x86_64-linux-gnu/intel-opencl/libigdrcl.so \
        > /opt/kmvms-intel/etc/OpenCL/vendors/intel.icd; \
    fi

RUN install -d -m 0755 /app/ai-model \
    && curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 --max-time 120 \
      https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx -o /app/ai-model/yolox_tiny.onnx \
    && curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 --max-time 30 \
      https://raw.githubusercontent.com/Megvii-BaseDetection/YOLOX/main/LICENSE -o /app/ai-model/LICENSE-YOLOX.txt \
    && printf '%s\n' \
      '427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7  /app/ai-model/yolox_tiny.onnx' \
      '0ec3668d3274bcf29e8a29e9576d5a2cd96fc78d3c5bec4387355a796e5d9088  /app/ai-model/LICENSE-YOLOX.txt' | sha256sum -c - \
    && chmod 0444 /app/ai-model/yolox_tiny.onnx /app/ai-model/LICENSE-YOLOX.txt
COPY . /app
RUN chmod a+rx /app/recorder_core /app/ai-model \
    && chmod a+r /app/recorder_core/__init__.py /app/recorder_core/ai_worker.py \
      /app/recorder_core/ai_engine.py /app/ai-model/model.json

CMD ["python", "-u", "main.py"]
