FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends adb tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY tv_bot.py /app/tv_bot.py

CMD ["python3", "/app/tv_bot.py"]
