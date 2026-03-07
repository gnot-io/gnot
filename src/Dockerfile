FROM python:3.11-slim

# Security: non-root user
RUN groupadd -r mesh && useradd -r -g mesh -d /app mesh

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

RUN chown -R mesh:mesh /app
USER mesh

EXPOSE 8080

ENTRYPOINT ["python", "node_runtime.py"]
CMD ["--config", "node-0/node.yaml"]
