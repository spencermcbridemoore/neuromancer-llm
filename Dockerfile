# App/orchestrator image. PostgreSQL is NATIVE on the VM (ADR-0041) and is NOT built here; the app
# reaches it via NEURO_DATABASE_URL. CI builds this image and boots it as a `neuro --version` smoke.
FROM python:3.12-slim

WORKDIR /app
COPY . .
# Build via hatchling + install the package and its deps; `neuro` lands on PATH.
RUN pip install --no-cache-dir .

CMD ["neuro", "--help"]
