FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends adb tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY tv_bot.py /app/tv_bot.py
COPY tv_control /app/tv_control

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python3", "/app/tv_bot.py", "--healthcheck"]

CMD ["python3", "/app/tv_bot.py"]
