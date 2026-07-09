FROM python:3.12-slim

WORKDIR /app

# Standard library only (urllib) — no dependencies to install.
COPY knowledge_processor.py /app/

CMD ["python", "-u", "knowledge_processor.py"]
