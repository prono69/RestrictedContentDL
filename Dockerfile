# --- STAGE 1: Builder ---
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build-essential only in the builder stage
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Build wheels into a local wheel cache directory
RUN pip install --no-cache-dir -U pip wheel==0.45.1 && \
    pip install --no-cache-dir --user -r requirements.txt


# --- STAGE 2: Runner ---
FROM python:3.12-slim AS runner

ENV TZ=Asia/Kolkata

# Install ONLY runtime dependencies (no compilers)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git tzdata ffmpeg wget && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy built Python packages from the builder stage
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy application files
COPY . .

CMD ["python3", "main.py"]
