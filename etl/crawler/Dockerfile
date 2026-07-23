# Chromium + Xvfb base so SeleniumBase uc mode can run "headed" in a container
FROM selenium/standalone-chromium:149.0

USER root

RUN apt-get update && \
    apt-get install -y \
    python3.11 \
    python3-pip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*


# Set the working directory in the container
WORKDIR /app

# Install dependencies first so code changes don't bust the pip cache layer
COPY Pipfile /app/
RUN python3 -m pip install pipenv && pipenv install

# Driver binaries for SeleniumBase. uc_driver is published for x86_64 only;
# on ARM we reuse the base image's system chromedriver (matching Chromium).
RUN if [ "$(uname -m)" = "x86_64" ]; then \
        pipenv run seleniumbase install chromedriver latest && \
        pipenv run seleniumbase install uc_driver latest; \
    else \
        DRIVERS_DIR=$(pipenv run python -c "import seleniumbase.drivers, os; print(os.path.dirname(seleniumbase.drivers.__file__))") && \
        cp /usr/bin/chromedriver "$DRIVERS_DIR/chromedriver"; \
    fi

# Copy the application code
COPY etl/crawler /app/crawler

ENV PYTHONPATH="${PYTHONPATH}:/app:/app/crawler"

EXPOSE 8080

CMD ["pipenv", "run", "python", "-u", "crawler/main.py"]
