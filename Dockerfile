FROM mcr.microsoft.com/playwright/python:v1.44.0-focal

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# CMD is overridden per Render service:
#   python -m based_inventory.jobs.quantity_alerts
#   python -m based_inventory.jobs.atc_audit
#   python -m based_inventory.jobs.weekly_snapshot
#   bash scripts/run_daily_velocity.sh   (velocity; PYBIN=python)
CMD ["python", "-m", "based_inventory.jobs.quantity_alerts"]
