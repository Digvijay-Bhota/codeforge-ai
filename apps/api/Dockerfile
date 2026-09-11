FROM python:3.12-slim

# Set environment variables for determinism and performance
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Create a non-root user
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN groupadd -r codeforge && useradd -r -g codeforge -d /app codeforge

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY --chown=codeforge:codeforge app/ app/
COPY --chown=codeforge:codeforge alembic/ alembic/
COPY --chown=codeforge:codeforge alembic.ini .

# Drop privileges
USER codeforge

# Add healthcheck targeting our new readiness endpoint
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

EXPOSE 8000

# Run the ASGI application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
