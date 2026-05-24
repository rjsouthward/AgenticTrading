# Finance GBrain — Layer 1 MemoryAgent (FastMCP streamable-HTTP server)
# Embeddings use the DigitalOcean API (no torch/sentence-transformers), so the image stays light.
FROM python:3.13-slim

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the self-contained memory service is needed
COPY FinAgents/memory/ ./FinAgents/memory/

WORKDIR /app/FinAgents/memory

EXPOSE 8000

# Stateless streamable-HTTP MCP app (config comes from env / fly secrets, not .env)
CMD ["uvicorn", "memory_server:app", "--host", "0.0.0.0", "--port", "8000"]
