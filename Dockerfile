FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY kubesleuth ./kubesleuth
RUN pip install --no-cache-dir ".[claude,mcp,live]" && useradd --uid 10001 --no-create-home kubesleuth

USER 10001
ENTRYPOINT ["kubesleuth"]
CMD ["--help"]
