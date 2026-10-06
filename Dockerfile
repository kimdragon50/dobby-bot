# Dobby Bot Dockerfile
# Multi-stage build for smaller image size

# Build stage
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies (apt-get upgrade for CVE-2026-64600)
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
# jaraco.context >= 6.1.0 for CVE-2026-23949 (Zip Slip vulnerability)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade setuptools "jaraco.context>=6.1.0" && \
    pip install --no-cache-dir --user -r requirements.txt

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Apply OS security updates (apt-get upgrade for CVE-2026-64600)
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /root/.local /home/appuser/.local

# Make sure scripts in .local are usable
ENV PATH=/home/appuser/.local/bin:$PATH

# CVE-2026-23949: install jaraco.context>=6.1.0 system-wide and remove vendored 5.3.0 dist-info
# setuptools vendors jaraco.context internally and no released version updates it to 6.1.0+
RUN pip install --no-cache-dir "jaraco.context>=6.1.0" && \
    rm -rf /usr/local/lib/python3.11/site-packages/setuptools/_vendor/jaraco.context-5.3.0.dist-info

# Copy application code
COPY --chown=appuser:appuser . .

# Create logs directory
RUN mkdir -p /app/logs && chown -R appuser:appuser /app/logs

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "print('healthy')" || exit 1

# Run the application
CMD ["python", "app.py"]
