FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

COPY requirements.txt .
# Keep the image compatible with docker-compose v1.29.2 and classic Docker builders.
# The requirements file pins a CPU Torch wheel so a CPU server does not pull CUDA runtimes.
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Ensure non-root user can write to /app (for volume mounts)
# and to the HF cache mount point (named volume inherits image dir ownership)
RUN mkdir -p /home/appuser/.cache/huggingface/hub && chown -R appuser:appuser /app /home/appuser/.cache

USER appuser

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
