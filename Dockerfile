FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 1000 --create-home drop
COPY app.py storage.py transfer.py remote.py ./
COPY static ./static
USER 1000:1000
CMD ["python", "app.py"]
