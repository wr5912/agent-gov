FROM python:3.11-slim

WORKDIR /e2e
COPY docker/e2e/slow_vllm.py /e2e/slow_vllm.py

EXPOSE 8000
CMD ["python", "/e2e/slow_vllm.py"]
