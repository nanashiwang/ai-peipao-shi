FROM python:3.11-slim

WORKDIR /app
COPY requirements.server.txt .
RUN pip install --no-cache-dir -r requirements.server.txt
COPY app ./app
COPY rpa ./rpa
COPY samples ./samples
COPY config/ark.example.json ./config/ark.example.json
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
