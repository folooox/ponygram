FROM python:3.11-slim

# ffmpeg is required by yt-dlp for video+audio merging
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent volumes: database and logs
VOLUME ["/app/data", "/app/logs"]

CMD ["python", "main.py"]
