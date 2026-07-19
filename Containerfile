# Stage 1: Build the Vue frontend
FROM node:lts-alpine AS build-stage
WORKDIR /app

# Copy package descriptors first to leverage caching
COPY frontend/package*.json ./frontend/
RUN cd frontend && npm install

# Copy frontend source and compile the build
COPY frontend/ ./frontend/
RUN cd frontend && npm run build

# Stage 2: Final lightweight execution environment
FROM python:3.12-alpine
WORKDIR /app

# Install FastAPI and Uvicorn runtime dependencies
RUN pip install --no-cache-dir fastapi uvicorn httpx python-dotenv

# Copy backend server code
COPY main.py ./

# Copy built frontend static files from the build stage
COPY --from=build-stage /app/frontend/dist ./frontend/dist

# Define data volume for config.json persistence
VOLUME /app/data

# Expose the API/Frontend port
EXPOSE 8000

# Start Flash Finder
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
