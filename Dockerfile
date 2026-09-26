FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY dealfinder dealfinder
RUN pip install --no-cache-dir . && rm -rf /root/.cache
USER 65534:65534
EXPOSE 8080
CMD ["python", "-m", "dealfinder"]
