FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY config.py .
COPY satellite ./satellite
COPY telemetry ./telemetry
COPY communication ./communication
COPY ground_station ./ground_station
COPY mission_control ./mission_control
COPY database ./database
COPY security ./security
COPY simulation ./simulation

RUN mkdir -p /app/data /app/logs && \
    useradd --create-home --shell /usr/sbin/nologin simulator && \
    chown -R simulator:simulator /app

USER simulator

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3)"

CMD ["python", "-m", "uvicorn", "mission_control.api:app", "--host", "0.0.0.0", "--port", "8000"]