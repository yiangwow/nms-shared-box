FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY box.py server.py ./
RUN mkdir /data && chown 1000:1000 /data
USER 1000
ENV NMS_RELAY_DATA=/data/box.json
CMD ["python", "-u", "server.py"]
