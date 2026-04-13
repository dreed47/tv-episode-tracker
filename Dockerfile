FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy scripts
COPY tracker.py auth.py ./

# Config and token are mounted at runtime — not baked into the image
VOLUME ["/config"]

CMD ["python", "tracker.py"]
