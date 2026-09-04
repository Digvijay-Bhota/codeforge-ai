FROM python:3.12-slim

# Set working directory inside the container
WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ app/

# Expose the port Uvicorn will listen on
EXPOSE 8000

# Run the ASGI application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
