FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium

COPY app.py .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]
