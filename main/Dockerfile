FROM python:3.11-slim

WORKDIR /app
COPY requirements.server.txt .
RUN pip install --no-cache-dir -r requirements.server.txt
COPY app ./app
COPY samples ./samples
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
