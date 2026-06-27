# Infrastructure Agent — Mockingbird

> **Scope:** Docker, Docker Compose, Kubernetes, CI/CD, monitoring, deployment, cloud infrastructure.

---

## Identity

You are the Infrastructure Agent for Mockingbird. Your responsibility is building the deployment infrastructure, container orchestration, CI/CD pipelines, monitoring, and cloud resource provisioning. You ensure the application runs reliably in development and production with minimal operational overhead.

---

## Tech Stack

| Technology | Purpose |
|-----------|---------|
| **Docker** | Container packaging |
| **Docker Compose** | Local development environment |
| **Kubernetes** | Production orchestration |
| **Helm** | K8s package management |
| **Terraform** | Cloud infrastructure provisioning |
| **GitHub Actions** | CI/CD pipelines |
| **Prometheus** | Metrics collection |
| **Grafana** | Dashboards and alerting |
| **Sentry** | Error tracking |
| **Fly.io / AWS EKS** | Cloud hosting |
| **CloudFlare** | CDN, DNS, DDoS protection |
| **NVIDIA GPU Operator** | K8s GPU management |

---

## Local Development Environment

### Docker Compose (Development)

```yaml
# docker-compose.yml — CPU-only development (no GPU required)
version: "3.9"

services:
  # PostgreSQL database
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: mockingbird
      POSTGRES_USER: mockingbird
      POSTGRES_PASSWORD: dev_password
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mockingbird"]
      interval: 5s
      timeout: 5s
      retries: 5

  # Redis (sessions, caching, Celery broker)
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

  # MinIO (S3-compatible local storage)
  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports:
      - "9000:9000"   # S3 API
      - "9001:9001"   # Console
    volumes:
      - minio_data:/data

volumes:
  postgres_data:
  redis_data:
  minio_data:
```

```yaml
# docker-compose.gpu.yml — Full stack with GPU inference
version: "3.9"

services:
  postgres:
    extends:
      file: docker-compose.yml
      service: postgres

  redis:
    extends:
      file: docker-compose.yml
      service: redis

  minio:
    extends:
      file: docker-compose.yml
      service: minio

  # Gateway (Python/FastAPI)
  gateway:
    build:
      context: ./gateway
      dockerfile: Dockerfile
    ports:
      - "3001:3001"
    environment:
      - DATABASE_URL=postgresql://mockingbird:dev_password@postgres:5432/mockingbird
      - REDIS_URL=redis://redis:6379
      - INFERENCE_SERVICE_URL=http://inference:8001
      - JWT_SECRET=dev_secret_change_in_production
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  # Inference Service (Python + GPU)
  inference:
    build:
      context: ./inference
      dockerfile: Dockerfile.gpu
    ports:
      - "8001:8001"
      - "50051:50051"  # gRPC
    environment:
      - REDIS_URL=redis://redis:6379
      - S3_ENDPOINT=http://minio:9000
      - S3_ACCESS_KEY=minioadmin
      - S3_SECRET_KEY=minioadmin
      - S3_BUCKET=mockingbird-models
      - DEVICE=cuda
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    depends_on:
      redis:
        condition: service_healthy

  # Celery Worker (training jobs)
  celery-worker:
    build:
      context: ./inference
      dockerfile: Dockerfile.gpu
    command: celery -A app.training.tasks worker --loglevel=info --concurrency=1
    environment:
      - REDIS_URL=redis://redis:6379
      - S3_ENDPOINT=http://minio:9000
      - S3_ACCESS_KEY=minioadmin
      - S3_SECRET_KEY=minioadmin
      - DEVICE=cuda
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    depends_on:
      redis:
        condition: service_healthy

  # Frontend (Python/FastAPI) — for production builds only
  # In development, run `uv run uvicorn app.main:app --reload` locally for hot reload
  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    ports:
      - "3000:3000"
    environment:
      - PUBLIC_API_URL=http://localhost:3001
      - PUBLIC_WS_URL=ws://localhost:3001
    depends_on:
      - gateway
```

---

## Dockerfiles

### Gateway (Python/FastAPI)

```dockerfile
# gateway/Dockerfile
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .

FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 3001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3001"]
```

### Inference Service (Python + GPU)

```dockerfile
# inference/Dockerfile.gpu
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

# Install Python 3.14
RUN apt-get update && apt-get install -y \
    python3.14 python3.14-venv \
    libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install Python dependencies with uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Download base models (HuBERT, Silero VAD, etc.)
COPY scripts/download_models.py scripts/
RUN python3 scripts/download_models.py

COPY . .

EXPOSE 8001 50051

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
```

### Frontend (Python/FastAPI)

```dockerfile
# frontend/Dockerfile
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 3000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
```

---

## CI/CD Pipeline (GitHub Actions)

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main, develop]

jobs:
  # Frontend tests
  frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.14"
      - run: cd frontend && uv sync
      - run: cd frontend && uv run ruff check .
      - run: cd frontend && uv run ruff format --check .
      - run: cd frontend && uv run mypy app/
      - run: cd frontend && uv run pytest

  # Gateway tests and build
  gateway:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_DB: mockingbird_test
          POSTGRES_USER: test
          POSTGRES_PASSWORD: test
        ports:
          - 5432:5432
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.14"
      - run: cd gateway && uv sync
      - run: cd gateway && uv run ruff check .
      - run: cd gateway && uv run mypy app/
      - run: cd gateway && uv run pytest
        env:
          DATABASE_URL: postgresql://test:test@localhost:5432/mockingbird_test
          REDIS_URL: redis://localhost:6379

  # Inference tests
  inference:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.14"
      - run: cd inference && uv sync
      - run: cd inference && uv run ruff check .
      - run: cd inference && uv run mypy app/
      - run: cd inference && uv run pytest tests/ -v --cov=app

  # Docker build verification
  docker-build:
    runs-on: ubuntu-latest
    needs: [frontend, gateway, inference]
    steps:
      - uses: actions/checkout@v4
      - run: docker compose build
```

```yaml
# .github/workflows/deploy.yml
name: Deploy

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      # Build and push Docker images
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      
      - uses: docker/build-push-action@v5
        with:
          context: ./frontend
          push: true
          tags: ghcr.io/${{ github.repository }}/frontend:${{ github.sha }}
      
      - uses: docker/build-push-action@v5
        with:
          context: ./gateway
          push: true
          tags: ghcr.io/${{ github.repository }}/gateway:${{ github.sha }}
      
      - uses: docker/build-push-action@v5
        with:
          context: ./inference
          file: ./inference/Dockerfile.gpu
          push: true
          tags: ghcr.io/${{ github.repository }}/inference:${{ github.sha }}
      
      # Deploy to Kubernetes
      - uses: azure/k8s-set-context@v3
        with:
          method: kubeconfig
          kubeconfig: ${{ secrets.KUBECONFIG }}
      
      - run: |
          kubectl set image deployment/frontend frontend=ghcr.io/${{ github.repository }}/frontend:${{ github.sha }}
          kubectl set image deployment/gateway gateway=ghcr.io/${{ github.repository }}/gateway:${{ github.sha }}
          kubectl set image deployment/inference inference=ghcr.io/${{ github.repository }}/inference:${{ github.sha }}
          kubectl rollout status deployment/frontend
          kubectl rollout status deployment/gateway
          kubectl rollout status deployment/inference
```

---

## Kubernetes Manifests

### Key Resources

```
infrastructure/k8s/
├── namespace.yaml
├── gateway/
│   ├── deployment.yaml        # 2-4 replicas, auto-scaling
│   ├── service.yaml           # ClusterIP
│   ├── hpa.yaml               # Horizontal Pod Autoscaler
│   └── configmap.yaml
├── inference/
│   ├── deployment.yaml        # GPU nodes, node affinity
│   ├── service.yaml           # ClusterIP (gRPC)
│   ├── hpa.yaml               # Scale on GPU utilization
│   └── configmap.yaml
├── frontend/
│   ├── deployment.yaml
│   ├── service.yaml
│   └── ingress.yaml           # CloudFlare Tunnel or nginx-ingress
├── monitoring/
│   ├── prometheus/
│   │   ├── deployment.yaml
│   │   ├── configmap.yaml     # Scrape configs
│   │   └── service.yaml
│   └── grafana/
│       ├── deployment.yaml
│       ├── configmap.yaml     # Dashboard JSON
│       └── service.yaml
├── databases/
│   ├── postgres-statefulset.yaml
│   └── redis-statefulset.yaml
└── secrets/
    └── sealed-secrets.yaml    # Encrypted secrets
```

### GPU Node Configuration

```yaml
# inference/deployment.yaml (key sections)
spec:
  template:
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      containers:
        - name: inference
          resources:
            limits:
              nvidia.com/gpu: 1
            requests:
              memory: "8Gi"
              cpu: "2"
```

---

## Monitoring & Alerting

### Grafana Dashboards

1. **Overview Dashboard**: Active connections, request rate, error rate, GPU utilization
2. **Latency Dashboard**: P50/P95/P99 audio latency, inference time, network time
3. **GPU Dashboard**: GPU memory usage, utilization %, temperature, model load times
4. **Training Dashboard**: Active training jobs, queue depth, success/failure rates
5. **Business Dashboard**: Active users, calls made, minutes consumed, revenue

### Alerting Rules

| Alert | Condition | Severity |
|-------|-----------|----------|
| High audio latency | P95 > 300ms for 5 min | Critical |
| GPU memory exhausted | Usage > 90% for 5 min | Warning |
| Inference service down | Health check fails for 30s | Critical |
| WebSocket error rate | > 5% for 5 min | Warning |
| Training job stuck | No progress for 30 min | Warning |
| Database connection pool | > 80% utilized | Warning |

---

## Environment Variables Template

```env
# .env.example

# === DATABASE ===
DATABASE_URL=postgresql://user:password@localhost:5432/mockingbird

# === REDIS ===
REDIS_URL=redis://localhost:6379

# === AUTH ===
JWT_SECRET=change-me-to-a-secure-random-string
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key

# === TWILIO ===
TWILIO_ACCOUNT_SID=ACxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_PHONE_NUMBER=+1234567890

# === STORAGE (S3 / MinIO) ===
S3_ENDPOINT=http://localhost:9000
S3_BUCKET=mockingbird-models
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin
AWS_REGION=us-east-1

# === INFERENCE ===
INFERENCE_SERVICE_URL=http://localhost:8001
INFERENCE_GRPC_URL=localhost:50051
DEVICE=cuda  # or 'cpu' for development without GPU

# === CLOUD API FALLBACK ===
INFERENCE_BACKEND=self_hosted  # or 'cartesia', 'elevenlabs'
CARTESIA_API_KEY=your-key
ELEVENLABS_API_KEY=your-key

# === FEATURE FLAGS ===
ENABLE_CALLING=true
ENABLE_RECORDING=false
ENABLE_TRAINING=true

# === MONITORING ===
SENTRY_DSN=https://xxx@sentry.io/xxx
PROMETHEUS_PORT=9090

# === FRONTEND ===
PUBLIC_API_URL=http://localhost:3001
PUBLIC_WS_URL=ws://localhost:3001
```

---

## Files to Create

```
infrastructure/
├── docker-compose.yml              # Development (CPU-only infra)
├── docker-compose.gpu.yml          # Full stack with GPU
├── k8s/
│   ├── namespace.yaml
│   ├── gateway/
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   └── hpa.yaml
│   ├── inference/
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   └── hpa.yaml
│   ├── frontend/
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   └── ingress.yaml
│   ├── monitoring/
│   │   ├── prometheus-config.yaml
│   │   └── grafana-dashboards.yaml
│   └── databases/
│       ├── postgres.yaml
│       └── redis.yaml
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── modules/
│       ├── eks/
│       ├── rds/
│       └── s3/
├── .env.example
└── scripts/
    ├── setup-dev.sh               # One-command dev setup
    ├── seed-db.sh                 # Database seeding
    └── download-models.sh         # Download ML model weights
```

---

## Implementation Order

1. **Docker Compose (dev)**: PostgreSQL + Redis + MinIO
2. **.env.example**: All environment variables documented
3. **Dockerfiles**: Frontend, Gateway, Inference
4. **Docker Compose (GPU)**: Full stack with GPU support
5. **CI pipeline**: Lint, test, build for all services
6. **K8s manifests**: Core deployments, services, ingress
7. **Monitoring**: Prometheus config, Grafana dashboards
8. **CD pipeline**: Build, push, deploy to K8s
9. **Terraform**: Cloud infrastructure (EKS, RDS, S3)
10. **Scripts**: Dev setup, DB seeding, model download
