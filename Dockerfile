FROM python:3.11-slim

# Set timezone early to avoid repetition
ENV TZ=Asia/Kolkata \
    HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Create user and set up environment in one layer
RUN useradd -m -u 1000 user && \
    mkdir -p $HOME/app $HOME/.cache && \
    # Install system dependencies
    apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        git build-essential linux-headers-amd64 tzdata && \
    # Set timezone
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    # Clean up
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    # Set permissions
    chown -R 1000:0 $HOME/app $HOME/.cache /usr && \
    chmod -R 777 $HOME/app /usr $HOME/.cache

WORKDIR $HOME/app

# Install Python dependencies in one layer
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip wheel==0.45.1 && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files and set permissions in one layer
COPY . .
RUN chown -R 1000:0 $HOME/app && \
    chmod -R 777 $HOME/app

EXPOSE 7860

CMD ["bash", "-c", "python3 server.py & python3 main.py"]