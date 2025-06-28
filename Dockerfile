FROM python:3.11-slim

RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
    git build-essential linux-headers-amd64 tzdata && \
    rm -rf /var/lib/apt/lists/*
    
# Create user with UID 1000 (Hugging Face requirement)
RUN useradd -m -u 1000 user
 
# Set environment variables for the non-root user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app 
RUN mkdir -p $HOME/.cache

# Adjust ownership/permissions for the app directory (and /usr if needed)
RUN chown -R 1000:0 $HOME/app $HOME/.cache /usr && \
    chmod -R 777 $HOME/app /usr $HOME/.cache
    
# Set timezone (use Asia/Kolkata if needed)
ENV TZ=Asia/Kolkata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN pip install --no-cache-dir -U pip wheel==0.45.1

COPY requirements.txt $HOME/app
RUN pip install -U -r requirements.txt

COPY . $HOME/app

# Adjust ownership/permissions for the app directory (and /usr if needed)
RUN chown -R 1000:0 $HOME/app $HOME/.cache /usr && \
    chmod -R 777 $HOME/app /usr $HOME/.cache

EXPOSE 7860

CMD ["bash", "-c", "python3 server.py & python3 main.py"]
