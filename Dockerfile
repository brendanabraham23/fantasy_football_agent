# Web UI for Hugging Face Spaces (Docker SDK) or any container host.
# Reports come from the repo's `data` branch; runs happen in GitHub Actions (see .github/workflows/predict.yml).
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Spaces run the container as uid 1000
RUN useradd -m -u 1000 app
WORKDIR /home/app/ffa

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app ffa ./ffa
COPY --chown=app:app config.json .

USER app
ENV PYTHONUNBUFFERED=1 \
    FFA_CACHE_DIR=/tmp/ffa-cache \
    FFA_DATA_DIR=/tmp/ffa-data \
    PORT=7860
EXPOSE 7860

# Set SLEEPER_USERNAME and FFA_DATA_REPO as Space variables; FFA_GITHUB_TOKEN and FFA_PASSWORD as secrets.
CMD ["sh", "-c", "exec python -m ffa ui --username \"$SLEEPER_USERNAME\" ${SLEEPER_LEAGUE_ID:+--league-id \"$SLEEPER_LEAGUE_ID\"} --host 0.0.0.0 --port \"$PORT\""]
