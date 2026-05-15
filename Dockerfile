# Use official Playwright Python image which has all browser dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=app.py
ENV PORT=5000

# Set work directory
WORKDIR /app

# Install system dependencies (build-essential for potential C-extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Browsers are already installed in the base image, but we ensure chromium is ready
RUN playwright install chromium

# Copy project
COPY . /app/

# Ensure necessary directories exist for persistence (will be mapped to volumes)
RUN mkdir -p /app/instance /app/static/uploads /app/data

# Expose port
EXPOSE 5000

# Run the application with Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--worker-class", "gthread", "--timeout", "1800", "--graceful-timeout", "1800", "--access-logfile", "-", "app:app"]
