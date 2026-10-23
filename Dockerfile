FROM golang:1.25 AS vpn-builder
WORKDIR /src
COPY vpnturn/ ./
RUN go mod download && CGO_ENABLED=0 go build -mod=readonly -trimpath -o /vpn-turn .

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 1000 --create-home drop
COPY app.py storage.py transfer.py remote.py builtin_turn.py vpn.py rooms.py entrypoint.py ./
COPY --from=vpn-builder /vpn-turn /usr/local/bin/fjorddrop-vpn-turn
COPY static ./static
ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["python", "app.py"]
