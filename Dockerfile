FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The initContainer runs: python -m app.migrate
# The main container runs: uvicorn app.main:app ...
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
