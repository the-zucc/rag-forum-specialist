FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY ingest.py /app/

ENV DEST_DIR=/app/threads

CMD ["python", "-u", "ingest.py", "--watch", "--wait-timeout", "0"]
