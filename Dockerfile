FROM python:3.12-slim

# git is the point of this app: it writes and serves generated repositories.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "exec hypercorn --bind 0.0.0.0:${PORT} 'app_translator.web.app:create_app()'"]
