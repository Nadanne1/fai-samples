# LandingAI Inference Service - Local Model Deployment and Testing Suite

This project provides a containerized environment for running and testing LandingAI's inference service locally. It enables developers to deploy machine learning models using Docker/Podman and test inference capabilities through a REST API interface.

The service simplifies the deployment of LandingAI models by providing a configurable Docker environment with health monitoring, volume mounting for model files, and a comprehensive test suite. It supports custom model bundles and offers flexible configuration options through environment variables, making it ideal for both development and testing scenarios.

## Repository Structure
```
components/landinglense.samples.LandingAi.Inference/tests/
├── .env.template               # Environment file template
├── docker-compose-local.yaml   # Docker Compose configuration for local deployment
├── run.sh                      # Main deployment script with environment validation
└── test-commands.txt           # Sample API test commands for verification
```

## Usage Instructions
### Prerequisites
- Docker or Podman installed and configured
- Bash shell environment
- curl command-line tool for testing
- A valid LandingAI model bundle file
- `.env` file with required configuration (copy from `.env.template`)

Required Environment Variables:
- `LOCAL_MODEL_ID`: Identifier for your model bundle
- `MODEL_VOLUME_PATH`: Local path to mount model files

### Installation

1. Clone the repository and navigate to the tests directory:
     ```bash
     cd components/landinglense.samples.LandingAi.Inference/tests
     ```

2. Create and configure the environment file:
     ```bash
     cp .env.template .env
     # Edit .env with your configuration
     ```

3. Ensure your model bundle is in the correct location:
     ```bash
     # Place your model bundle in the specified MODEL_VOLUME_PATH
     # Format: bundle_<LOCAL_MODEL_ID>.zip
     ```

### Quick Start

1. Start the inference service:
     ```bash
     ./run.sh
     ```

2. Verify the service is running:
     ```bash
     curl http://localhost:8000/status
     ```

3. Stop the service:
     ```bash
     ./run.sh --down
     ```

### More Detailed Examples

Testing Image Inference:
> ℹ️ **Note**: Make sure the chagne input for file to yours.
```bash
# Send an image for inference
curl -X 'POST' 'http://localhost:8000/images' \
  -H 'accept: */*' \
  -H 'Content-Type: multipart/form-data' \
  -F 'file=@model/images/test.jpg;type=image/jpeg' \
  -F 'file_url=' \
  -F 'metadata='
```

### Troubleshooting

Common Issues:

1. Service Fails to Start
     ```bash
     # Check Docker logs
     docker logs ${CONTAINER_NAME}

     # Verify environment variables
     ./run.sh | grep Configuration
     ```

2. Health Check Failures
- Ensure port 8000 is not in use
- Check model bundle format and location
- Verify network connectivity if using custom network mode

3. Model Loading Issues
     ```bash
     # Verify model path
     ls -l ${MODEL_VOLUME_PATH}/bundle_${LOCAL_MODEL_ID}.zip

     # Check container volume mounting
     docker inspect ${CONTAINER_NAME} | grep Mounts -A 10
     ```

## Infrastructure

Docker Resources:
- Container: landinglens
  - Image: public.ecr.aws/landing-ai/deploy:latest (configurable)
  - Port: 8000 (configurable)
  - Volume: Model files mounted at /data/model
  - Health Check: 30s interval with 3 retries
  - Network: Configurable via NETWORK_MODE

## Deployment

Prerequisites:
- Docker/Podman installed
- Model bundle file
- Configured .env file

Deployment Steps:
1. Configure environment variables in .env
2. Place model bundle in volume path
3. Run ./run.sh to start service
4. Verify deployment with health check endpoint

Environment Configuration:
- PORT: Service port (default: 8000)
- NETWORK_MODE: Docker network configuration
- CONTAINER_NAME: Custom container name
- LANDING_IMAGE: Custom image override
- ADDITIONAL_ARGS: Extra runtime arguments