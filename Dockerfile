FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY bot.py .

# Railway injects $PORT; default to 5000 for local runs
ENV PORT=5000

EXPOSE 5000

CMD ["python", "bot.py"]
