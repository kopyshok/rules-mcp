FROM python:3.12-slim

# git нужен, чтобы забирать правила из GitLab; ca-certificates — для https
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rules_index.py server.py test_rules_index.py ./

ENV RULES_DIR=/data/rules \
    PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["python", "server.py"]
