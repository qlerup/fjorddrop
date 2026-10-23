FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends coturn ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY turn/start.py /opt/turn/start.py
ENTRYPOINT ["python", "/opt/turn/start.py"]
