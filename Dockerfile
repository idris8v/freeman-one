FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
RUN apt-get update -qq && apt-get install -y --no-install-recommends postgresql-client-17 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --create-home bot
COPY app app
COPY scripts/check_assets.py /tmp/check_assets.py
RUN python /tmp/check_assets.py --root /app && rm /tmp/check_assets.py
RUN mkdir data && chown -R bot:bot /app
USER bot
EXPOSE 8000
CMD ["python", "-m", "app.main"]
