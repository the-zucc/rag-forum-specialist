FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so code changes don't bust the pip cache layer
COPY Pipfile /app/
RUN pip install --no-cache-dir pipenv && pipenv install --skip-lock

COPY rag-agent/rag.py /app/rag-agent/rag.py

ENTRYPOINT ["pipenv", "run", "python", "-u", "rag-agent/rag.py"]
