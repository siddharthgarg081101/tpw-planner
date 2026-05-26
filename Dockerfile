# TPW Planner backend — Render.com target (pure compute, no Google APIs)
# Python 3.11 slim. Same Dockerfile works for any container host that injects
# a PORT env var (Render, Cloud Run, Fly, Railway, etc.).

FROM python:3.11-slim
WORKDIR /app

# Install deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY *.py ./

# Render / Cloud Run / Fly all inject PORT. Default to 8080 locally.
ENV PORT=8080

# Shell form so $PORT expands at runtime.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
