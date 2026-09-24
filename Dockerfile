FROM python:3.11-slim

# Create a non-root user with a specific UID
RUN useradd -m -u 1000 appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY release_arr.py .

# Change ownership of the app directory
RUN chown -R appuser:appuser /app

# Switch to the non-root user
USER appuser

CMD ["python", "-u", "release_arr.py"]
