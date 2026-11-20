FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends p7zip-full unar && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
COPY requirements-dameng.txt .
RUN pip install --no-cache-dir --index-url https://mirrors.aliyun.com/pypi/simple/ \
    --only-binary=:all: --no-deps -r requirements-dameng.txt
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/dmssl:/usr/local/lib/python3.12/site-packages/dmpython.libs
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
