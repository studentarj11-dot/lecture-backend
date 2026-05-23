# ── Stage: Runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim

# Install ffmpeg (required by yt-dlp for audio conversion)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for audio storage
RUN mkdir -p static/audio

# Expose port (Render sets $PORT automatically)
EXPOSE 5000

# Start with gunicorn (production WSGI server)
# gunicorn handles the app; APScheduler runs inside the worker process
CMD ["gunicorn", "--bind", "0.0.0.0:$PORT", "--workers", "1", "--timeout", "300", "app:app"]
