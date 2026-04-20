FROM python:3.11-slim

# ffmpeg is the only non-Python dependency
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY frontend/ ../frontend/

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
