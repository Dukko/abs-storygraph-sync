FROM python:3.12-slim

# Install Chrome and ChromeDriver
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV PORT=8080

EXPOSE 8080

CMD ["python", "app.py"]
