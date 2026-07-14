FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["analytics-mcp-hosted"]
