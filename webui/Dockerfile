FROM python:3.11-slim

WORKDIR /app

# One static file, no build step, no dependencies — served as-is.
COPY index.html /app/

EXPOSE 3000

CMD ["python3", "-m", "http.server", "3000"]
