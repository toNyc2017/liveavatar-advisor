# LiveAvatar Advisor — runtime container.
#
# Built and pushed to ECR by .github/workflows/deploy-backend.yml (phase C).
# App Runner pulls this image and runs it. On cold start the container
# hydrates ChromaDB from the S3 seed tarball pointed at by
# CHROMA_SEED_S3_URI; per-visitor memory lives in the S3 bucket pointed
# at by USERS_S3_BUCKET when USERS_STORAGE_BACKEND=s3.
#
# Local verification:
#   docker build -t liveavatar .
#   docker run --rm --env-file .env -p 8000:8000 liveavatar
# Then open http://localhost:8000 — should behave identically to ./run.sh.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install runtime deps first so subsequent code-only changes don't bust
# this layer's cache. The lean requirements-runtime.txt omits offline
# tooling deps (mediapipe, opencv, yt-dlp, python-pptx, etc.) — keeps
# the image ~600MB instead of ~2GB.
COPY requirements-runtime.txt ./
RUN pip install -r requirements-runtime.txt

# Application code.
COPY advisor_backend.py storage.py index.html ./
COPY scripts/ ./scripts/

EXPOSE 8000

# App Runner uses HTTP /health for the readiness probe. Keep this in
# sync with the route registered in advisor_backend.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        sys.exit(0) if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status == 200 else sys.exit(1)" \
    || exit 1

# --proxy-headers + --forwarded-allow-ips so X-Forwarded-For from CloudFront
# (and the App Runner frontproxy) is honored — the access log gets the real
# client IP instead of the proxy's egress address.
CMD ["uvicorn", "advisor_backend:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
