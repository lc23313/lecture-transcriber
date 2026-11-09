FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
ENV IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY services ./services
COPY static ./static
RUN mkdir -p /app/data /app/settings
ENV LT_CONFIG_FILE=/app/settings/config.json
EXPOSE 8765
CMD ["python","-m","uvicorn","app:app","--host","0.0.0.0","--port","8765","--proxy-headers","--forwarded-allow-ips","*"]
