FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
ENV STATE_DB=/data/state.db PORT=7860
RUN mkdir -p /data && chmod 777 /data
EXPOSE 7860
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
