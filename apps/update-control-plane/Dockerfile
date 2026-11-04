FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8

RUN apk add --no-cache curl

WORKDIR /opt/kmvms-control
COPY control_plane.py /opt/kmvms-control/control_plane.py

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "/opt/kmvms-control/control_plane.py"]
