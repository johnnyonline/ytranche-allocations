FROM python:3.12

WORKDIR /app

# Copy source
COPY . .

# Install deps outside /app, so `docker run -v "$PWD:/app"` can mount the repo over it
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install uv && uv sync --no-dev --no-cache

# Ensure the project venv is on PATH
ENV PATH="/opt/venv/bin:${PATH}"

# Default entrypoint
ENTRYPOINT ["python", "-u", "-m", "allocations"]
