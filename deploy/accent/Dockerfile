# Сервис ударений для озвучки (RUAccent). Включается строкой COMPOSE_PROFILES=accent в deploy/.env
FROM python:3.12-slim
RUN pip install --no-cache-dir ruaccent==1.5.8.3 aiohttp==3.13.2
ENV HF_HOME=/models
COPY server.py /app/server.py
CMD ["python", "/app/server.py"]
