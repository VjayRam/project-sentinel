FROM python:3.11-slim

WORKDIR /app

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

COPY services/classifier/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/classifier/main.py .

# Model artifacts are mounted at runtime via K8s volume or built-in via CI.
# Default paths match what optimize.py produces under models/onnx_quantized/.
ENV MODEL_PATH=/models/onnx_quantized/model.onnx
ENV TOKENIZER_PATH=/models/onnx_quantized
ENV MODEL_VERSION=v1

USER appuser

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
