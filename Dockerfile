FROM python:3.11-slim

WORKDIR /app

# Install CPU-only Python deps
COPY api/requirements.txt /app/api/requirements.txt
RUN pip install --no-cache-dir -r /app/api/requirements.txt

# Copy the waspada package (Python backend)
COPY waspada/ /app/waspada/
COPY api/ /app/api/
COPY tests/ /app/tests/

# Copy the pre-built dashboard (must run `npx vite build` in dashboard/ first)
COPY dashboard/dist/ /app/dashboard/dist/
COPY dashboard/fixtures/ /app/dashboard/fixtures/

# Set Python path so waspada is importable
ENV PYTHONPATH=/app

EXPOSE 8080

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
