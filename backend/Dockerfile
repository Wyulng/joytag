FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

COPY requirements.txt .
# BuildKit cache mount: pip wheels persist on host across builds —
# requirements changes trigger reinstall but no re-download.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Ensure non-root user can write to /app (for volume mounts)
# and to the HF cache mount point (named volume inherits image dir ownership)
RUN mkdir -p /home/appuser/.cache/huggingface/hub && chown -R appuser:appuser /app /home/appuser/.cache

USER appuser

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
