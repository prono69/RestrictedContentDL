FROM python:3.11-slim

# Set timezone
ENV TZ=Asia/Kolkata

# Install system dependencies (including ffmpeg)
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        git build-essential linux-headers-amd64 tzdata ffmpeg wget && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip wheel==0.45.1 && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

EXPOSE 8080

CMD ["bash", "-c", "python3 server.py & python3 main.py"]