FROM python:3.12-bookworm

# System deps for headless browsers (Camoufox / Firefox)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 libdbus-glib-1-2 libx11-xcb1 libxt6 \
    libasound2 libpango-1.0-0 libcairo2 libatk1.0-0 \
    fonts-liberation xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build context is the repo root so the shared/ package can be copied in
# alongside the service. Paths below are relative to the repo root.
COPY responder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && scrapling install

COPY responder/*.py .
COPY shared ./shared

RUN mkdir -p data

CMD ["python", "-u", "responder.py"]
