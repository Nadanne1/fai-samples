# Landing AI Edge Intelligence with AWS IoT Greengrass: Complete Edge AI Inference Solution

> Deploy computer vision inference at the edge with Landing AI models on AWS IoT Greengrass-enabled devices

A comprehensive suite that seamlessly integrates Landing AI's computer vision capabilities with AWS IoT Greengrass for edge deployments. This solution enables reliable AI inference directly on edge devices with automatic model synchronization from S3 buckets and real-time prediction publishing to AWS IoT Core.

## 🏗️ Solution Architecture

![Architecture Diagram](./docs/images/architecture-diagram.drawio.svg)

### Component Overview

| Component                | Purpose                 | Key Features                                           |
| ------------------------ | ----------------------- | ------------------------------------------------------ |
| **S3 Downloader**        | Manages model artifacts | Large file support, versioning                         |
| **Landing AI Inference** | Runs inference engine   | Docker containerization, REST API, multi-model support |
| **Landing AI Predictor** | Processes results       | Event filtering, MQTT publishing, metadata enrichment  |

### Data Flow

1. **Model Deployment**: Models trained in Landing AI platform are exported and stored in S3
2. **Edge Synchronization**: S3 Downloader fetches models to edge device on command
3. **Inference**: Landing AI container loads models and performs inference on input images
4. **Results Processing**: Predictor component filters, enriches and publishes results
5. **Cloud Integration**: Predictions flow to AWS IoT Core for further processing or storage

## Repository Structure
```
.
├── build_all.sh                                     # Main build script for all components and Docker images
├── publish_all.sh                                   # Publishes components and Docker images with change detection
├── components/                                      # Greengrass component source code
│   ├── aws.samples.S3Downloader/                    # S3 download management component
│   │   ├── main.py                                  # Entry point for S3 download service
│   │   ├── gdk-config.json                          # GDK configuration
│   │   ├── recipe.yaml                              # Component deployment configuration
│   │   ├── requirements.txt                         # Python dependencies
│   │   ├── src/                                     # Core S3 download functionality
│   │   │   ├── utils/
│   │   │   │   └── logging_config.py                # Log config
│   │   │   ├── greengrass_mqtt.py                   # Greengrass MQTT client implementation
│   │   │   ├── mock_mqtt.py                         # Mock MQTT client for testing
│   │   │   ├── mqtt_interface.py                    # MQTT interface definition
│   │   │   ├── model_shadow_manager.py              # Model metadata shadow management
│   │   │   ├── s3_command_service.py                # S3 command handling service
│   │   │   ├── s3_download_manager.py               # S3 download management
│   │   │   └── s5cmd_async.py                       # Async s5cmd wrapper
│   │   └── tests/                                   # Component test suite
│   ├── landinglense.samples.LandingAi.Inference/    # AI inference runtime component
│   │   ├── docker-compose.yaml                      # Container configuration for inference
│   │   ├── gdk-config.json                          # GDK configuration
│   │   ├── recipe.yaml                              # Component deployment configuration
│   │   └── tests/                                   # Component test suite
│   └── landinglense.samples.LandingAi.Predictor/    # AI prediction processing component
│       ├── main.py                                  # Entry point for prediction service
│       ├── gdk-config.json                          # GDK configuration
│       ├── recipe.yaml                              # Component deployment configuration
│       ├── requirements.txt                         # Python dependencies
│       ├── src/                                     # Core prediction functionality
│       │   ├── utils/
│       │   │   └── logging_config.py                # Log config
│       │   ├── greengrass_mqtt.py                   # Greengrass MQTT client implementation
│       │   ├── mock_mqtt.py                         # Mock MQTT client for testing
│       │   ├── mqtt_interface.py                    # MQTT interface definition
│       │   └── predictor.py                         # Run prediction
│       └── tests/                                   # Component test suite
└── docker/                                          # Docker image build configurations
```

## ⚡ Quick Start

>
> To be added
>

## Usage Instructions
### Prerequisites

#### Edge Device Requirements:
- AWS IoT Greengrass Core v2.0 or later installed
- Linux-based operating system (for s5cmd compatibility)
  - Windows Subsystem for Linux (WSL) can be used
- Docker Engine 20.10.x or later
- Proper IAM role configuration
- Sufficient disk space for downloads

#### IAM Role Permissions
The following IAM policies must be attached to your AWS IoT Greengrass Role Alias:

1. S3 Access Policy for Model Downloads:
    ```json
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": [
                    "s3:GetObject",
                    "s3:ListBucket"
                ],
                "Resource": [
                    "*"
                ],
                "Effect": "Allow"
            }
        ]
    }
    ```

2. Docker Application Manager ECR Access:
    ```json
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer"
                ],
                "Resource": [
                    "*"
                ],
                "Effect": "Allow"
            }
        ]
    }
    ```

3. CloudWatch Logs Access:
    ```json
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams"
                ],
                "Effect": "Allow",
                "Resource": "arn:aws:logs:*:*:*"
            }
        ]
    }
    ```

For additional component-specific permissions, refer to the [AWS IoT Greengrass V2 public components documentation](https://docs.aws.amazon.com/greengrass/v2/developerguide/public-components.html).

#### Development Environment Requirements:
- Python 3.7 or later
- AWS CLI v2 configured with appropriate permissions:
  - AWS IoT Core access
  - Amazon ECR access
  - Amazon S3 access
  - AWS IoT Greengrass permissions
- AWS IoT Greengrass Development Kit (GDK) installed
- AWS S3 bucket access permissions

#### Required Python Packages:
- landingai
- pillow
- awsiotsdk >= 1.17.0
- awsiot >= 0.1.0

### Installation

1. Clone the repository:
    ```bash
    git clone <repository-url>
    cd <repository-name>
    ```

2. Configure AWS credentials:
    ```bash
    aws configure
    ```

3. Create a `.env` file by copying `.env.template` and updating the values:
    ```bash
    cp .env.template .env
    ```

    Then edit the `.env` file with your values:

    ```bash
    ECR_REPO=<your-ecr-repository>
    AWS_REGION=<your-region>
    ```

4. Verifiy AWS IoT Greengrass Development Kit (GDK) configuration Update region to your desired region.
    > ℹ️ **Note**: gdk-config.json of all three components need to be changed.
    ```json
    {
      "component": {
        "aws.samples.S3Downloader": {
          "author": "Amazon Web Services",
          "version": "NEXT_PATCH",
          "build": {
            "build_system": "zip",
            "options": {
              "zip_name": "main-artifact",
              "excludes": [
                "**/*.pyc",
                "**/__pycache__",
                "**/.pytest_cache",
                "**/tests/",
                "**/tests/*",
                "**/.git/",
                "**/.git/*",
                "**/.venv",
                "**/.env",
                "**/.!*"
              ]
            }
          },
          "publish": {
            "bucket": "greengrass-components",
            "region": "<your-region>"
          }
        }
      },
      "gdk_version": "1.3.0"
    }
    ```

5. Build all components and Docker images:
    ```bash
    ./build_all.sh
    ```

6. Publish components and Docker images:
    ```bash
    ./publish_all.sh
    ```

### Quick Start

1. Deploy the S3 Downloader component:
    ```bash
    aws greengrassv2 create-deployment \
      --target-arn "arn:aws:iot:region:account:thing/thing-name" \
      --components '{"aws.samples.S3Downloader":{"componentVersion":"1.0.0"}}'
    ```

2. Deploy the Landing AI components:
    ```bash
    aws greengrassv2 create-deployment \
      --target-arn "arn:aws:iot:region:account:thing/thing-name" \
      --components '{
        "landinglense.samples.LandingAi.Inference":{"componentVersion":"1.0.0"},
        "landinglense.samples.LandingAi.Predictor":{"componentVersion":"1.0.0"}
      }'
    ```

### Deployment Steps

1. Deploy the S3 Downloader component:
    ```bash
    aws greengrassv2 create-deployment \
      --target-arn "arn:aws:iot:region:account:thing/thing-name" \
      --components '{"aws.samples.S3Downloader":{"componentVersion":"1.0.0"}}'
    ```

2. Upload your Landing AI model to S3:
    ```bash
    aws s3 cp bundle_model.zip s3://my-model-bucket/models/
    ```

3. Send download command to S3 Downloader:
    ```bash
    aws iot-data publish \
      --topic "s3downloader/device-1/commands" \
      --payload '{
        "command": "download",
        "bucket": "my-model-bucket",
        "key": "models/bundle_model.zip",
        "destination": "/data/downloads/model/aircraft_movement"
      }'
    ```

4. Verify the model was deployed:
    ```bash
    aws iot-data publish \
      --topic "s3downloader/device-1/commands" \
      --payload '{"command": "list", "type": "models"}'
    ```

5. Deploy the Landing AI Inference component:
    ```bash
    aws greengrassv2 create-deployment \
      --target-arn "arn:aws:iot:region:account:thing/thing-name" \
      --components '{"landinglense.samples.LandingAi.Inference":{"componentVersion":"1.0.0"}}'
    ```

6. Verify the inference API is running:
    ```bash
    curl http://localhost:8000/status
    ```

7. Deploy the Landing AI Predictor component:
    ```bash
    aws greengrassv2 create-deployment \
      --target-arn "arn:aws:iot:region:account:thing/thing-name" \
      --components '{"landinglense.samples.LandingAi.Predictor":{"componentVersion":"1.0.0"}}'
    ```

8. Test the predictor by sending an inference request:
    ```bash
    aws iot-data publish \
      --topic "predictor/device-1/commands" \
      --payload '{"image_path":"/home/ubuntu/model/images/test.5jqkq5ag.jpg"}'
    ```

### More Detailed Examples

1. Download a model from S3:
    ```bash
    aws iot-data publish \
      --topic "s3downloader/device-1/commands" \
      --payload '{
        "command": "download",
        "bucket": "my-model-bucket",
        "key": "models/aircraft-detection-v1.zip",
        "destination": "/data/downloads/model/aircraft_movement"
      }'
    ```

2. Check model download status:
    ```bash
    aws iot-data publish \
      --topic "s3downloader/device-1/commands" \
      --payload '{
        "command": "list",
        "type": "downloads"
      }'
    ```

### Troubleshooting

1. S3 Download Issues
- Check AWS credentials and permissions
- Verify network connectivity
- Review logs:
    ```bash
    tail -f /greengrass/v2/logs/aws.samples.S3Downloader.log
    ```

2. Inference Engine Issues
- Check Docker container status:
    ```bash
    docker ps | grep ll_inference
    ```
- Review inference logs:
    ```bash
    docker logs ll_inference
    ```

3. Predictor Issues
- Verify MQTT connectivity:
    ```bash
    tail -f /greengrass/v2/logs/landinglense.samples.LandingAi.Predictor.log
    ```
- Check topic subscriptions in AWS IoT Core