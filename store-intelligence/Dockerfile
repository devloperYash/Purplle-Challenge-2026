FROM python:3.11-slim

WORKDIR /app

# System dependencies for OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY data/ ./data/
COPY dashboard/ ./dashboard/

# Create events output directory
RUN mkdir -p events

WORKDIR /app

EXPOSE 8000

ENV DB_PATH=/app/data/store_intelligence.db
ENV PYTHONPATH=/app/app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
