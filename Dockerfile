# Small official Python image.
FROM python:3.11-slim

# System packages we need:
#   ffmpeg            -> compile.py builds the video with it
#   tini              -> a tiny init that forwards signals so the container stops cleanly
#   tzdata            -> timezone database so America/New_York etc. resolve correctly
#   fonts-dejavu-core -> a font for compile.py's optional date/time overlay
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini tzdata fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Print Python logs immediately so `docker logs` shows live output (not buffered).
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies first so Docker can cache this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY app/ ./app/
COPY compile.py login_once.py ./

# /data holds snapshots, logs, state and output (bind-mounted to UnRaid).
VOLUME ["/data"]

# tini runs as PID 1 and forwards signals; the default command runs the daemon.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.daemon"]
