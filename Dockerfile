# Use Python 3.9
FROM python:3.9-slim

# Set up workdir
WORKDIR /code

# Install system dependencies for audio/tools
RUN apt-get update && apt-get install -y \
    ffmpeg \
    portaudio19-dev \
    python3-all-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY ./requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Copy all files
COPY . .

# Hugging Face Spaces port is 7860
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
