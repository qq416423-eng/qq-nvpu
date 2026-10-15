FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests websocket-client
COPY bot_hf.py .
EXPOSE 7860
CMD ["python", "-u", "bot_hf.py"]
