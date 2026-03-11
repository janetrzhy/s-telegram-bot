FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["gunicorn", "bot:app", "--bind", "0.0.0.0:10000", "--timeout", "120"]
