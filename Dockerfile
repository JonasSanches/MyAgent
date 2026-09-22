FROM python:3.11-slim

WORKDIR /app
COPY . /app

ENV PYTHONPATH=/app/src
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8787
CMD ["python", "-m", "dev_agent.server", "--host", "0.0.0.0"]
