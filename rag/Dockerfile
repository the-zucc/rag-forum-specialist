FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so code changes don't bust the pip cache layer
COPY Pipfile /app/
RUN pip install --no-cache-dir pipenv && pipenv install --skip-lock

COPY rag/*.py /app/rag/

ENTRYPOINT ["pipenv", "run", "python", "-u", "rag/rag.py"]
