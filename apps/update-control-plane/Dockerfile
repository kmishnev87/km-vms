FROM python:3.12-alpine

WORKDIR /opt/kmvms-control
COPY control_plane.py /opt/kmvms-control/control_plane.py

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "/opt/kmvms-control/control_plane.py"]
