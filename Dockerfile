# Official Microsoft Playwright Python image pre-configured with Chromium & system dependencies
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# Set working directory
WORKDIR /app

# Copy application files
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy remaining code
COPY . .

# Expose Web Service Port
EXPOSE 5000

# Start Application with Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "main:app"]
