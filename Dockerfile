FROM python:3.12-slim

WORKDIR /app

# ffmpeg нужен для сборки шортсов
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# запасной голос для шортсов (Piper) — на случай, если голос Microsoft не отвечает
COPY shorts/get_voice.py /tmp/get_voice.py
RUN python /tmp/get_voice.py /opt/piper

COPY . .

CMD ["python", "bot.py"]
