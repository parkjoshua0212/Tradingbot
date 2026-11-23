# arm64: the Lambda runs on Graviton (cheaper per GB-s) and the build hosts (Apple Silicon, GitHub arm runners) are arm64.
# Build: docker build --platform linux/arm64 --provenance=false -t tradingbot .  (Lambda rejects buildx attestation manifests)
FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.13

ENV PYTHONUTF8=1
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt --target ${LAMBDA_TASK_ROOT}
COPY *.py config.toml ${LAMBDA_TASK_ROOT}/

CMD ["lambda_handler.handler"]
