# LandingAI Predictor: MQTT-Based Image Classification Testing Framework

The LandingAI Predictor is a testing framework designed to validate and interact with image classification services over MQTT. It provides a robust interface for sending image processing commands, monitoring service status, and receiving inference results in real-time.

This framework enables developers to test image classification services by simulating MQTT broker interactions and providing both programmatic and command-line interfaces. The system supports asynchronous operations, status monitoring, and detailed inference result analysis, making it ideal for development, testing, and integration validation of AI-powered image classification services.

## Repository Structure
```
landinglense.samples.LandingAi.Predictor/
├── tests/                      # Test suite directory containing test implementations
│   └── test_predictor.py       # Main test implementation with MQTT client and CLI interface
```

## Usage Instructions
### Prerequisites
- Python 3.7 or higher
- Async IO support
- MQTT broker (real or mock implementation)
- Access to image files for testing

Required Python packages:
```bash
landingai
pillow
awsiotsdk>=1.17.0
```

### Installation
1. Clone the repository:
```bash
git clone <repository-url>
cd components/landinglense.samples.LandingAi.Predictor
```

2. Install dependencies:
```bash
python -m venv .venv
. ".venv/bin/activate"
python -m pip install -r requirements.txt
```

### Start landinglense.samples.LandingAi.Inference

Refer to the [Usage Instructions](../../landinglense.samples.LandingAi.Inference/tests/README.md#usage-instructions) section of [LandingAI Inference Service - Local Model Deployment and Testing Suite](../../landinglense.samples.LandingAi.Inference/tests/README.md#usage-instructions) and start LandingAI's inference service locally.

### Using the CLI interface: 
```bash
python -m tests.test_predictor
```

Available commands:
```
help                    - Show help message
status                 - Show current predictor status
process <image_path>   - Process an image file
exit/quit              - Exit the program
```

Sample execution:
```
landingai-greengrass-samples/components/landinglense.samples.LandingAi.Predictor ❯ python -m tests.test_predictor
20XX-XX-XX 21:13:42,408 - __main__ - INFO - Starting test with mock MQTT client
20XX-XX-XX 21:13:42,408 - src.predictor - INFO - Initializing Image Classification Service
20XX-XX-XX 21:13:42,408 - src.predictor - INFO - Initializing EdgePredictor: localhost:8000
20XX-XX-XX 21:13:42,411 - src.mock_mqtt - INFO - Mock MQTT: Connected
20XX-XX-XX 21:13:42,411 - src.mock_mqtt - INFO - Mock MQTT: Subscribed to device/test-device/status
20XX-XX-XX 21:13:42,411 - src.mock_mqtt - INFO - Mock MQTT: Subscribed to device/test-device/inference_results
20XX-XX-XX 21:13:42,411 - __main__ - INFO - Test client connected and subscribed to response topics
20XX-XX-XX 21:13:42,411 - src.predictor - INFO - Starting Image Classification Service...
20XX-XX-XX 21:13:42,411 - src.mock_mqtt - INFO - Mock MQTT: Connected
20XX-XX-XX 21:13:42,411 - src.mock_mqtt - INFO - Mock MQTT: Subscribed to device/test-device/commands
20XX-XX-XX 21:13:42,411 - src.mock_mqtt - INFO - Mock MQTT: Publishing to device/test-device/status: {"status": "READY", "timestamp": "20XX-05-20T04:13:42.411341+00:00", "deviceId": "test-device"}
20XX-XX-XX 21:13:42,411 - __main__ - INFO - Received status: {"status": "READY", "timestamp": "20XX-05-20T04:13:42.411341+00:00", "deviceId": "test-device"}
20XX-XX-XX 21:13:42,411 - src.predictor - INFO - Service started successfully
20XX-XX-XX 21:13:42,411 - __main__ - INFO - Waiting for READY status...
20XX-XX-XX 21:13:42,411 - __main__ - INFO - Predictor is ready

Image Classification Test CLI
============================
Type 'help' for available commands
command> help  

Available Commands:
  help                                   - Show this help message
  status                                 - Show current status
  process <image_path>                   - Process an image
  exit/quit                              - Exit the program

command> status
Checking status...
Predictor status: READY

Recent status messages:
  1. Status: READY
     Timestamp: 20XX-XX-XXT04:13:42.411341+00:00
     Device ID: test-device

command> process /dev/landingai-greengrass-samples/components/landinglense.samples.LandingAi.Inference/tests/model/images/test.5jqkq5ag.jpg
Sending command to process image: /dev/landingai-greengrass-samples/components/landinglense.samples.LandingAi.Inference/tests/model/images/test.5jqkq5ag.jpg
20XX-XX-XX 21:16:32,127 - __main__ - INFO - Sending command to process image: /dev/landingai-greengrass-samples/components/landinglense.samples.LandingAi.Inference/tests/model/images/test.5jqkq5ag.jpg
20XX-XX-XX 21:16:32,128 - src.mock_mqtt - INFO - Mock MQTT: Publishing to device/test-device/commands: {"image_path": "/dev/landingai-greengrass-samples/components/landinglense.samples.LandingAi.Inference/tests/model/images/test.5jqkq5ag.jpg", "timestamp": 1747714592.127347}
20XX-XX-XX 21:16:32,128 - src.predictor - INFO - Processing message on topic device/test-device/commands: {'image_path': '/dev/landingai-greengrass-samples/components/landinglense.samples.LandingAi.Inference/tests/model/images/test.5jqkq5ag.jpg', 'timestamp': 1747714592.127347}
20XX-XX-XX 21:16:32,128 - src.predictor - INFO - Received request to process image: /dev/landingai-greengrass-samples/components/landinglense.samples.LandingAi.Inference/tests/model/images/test.5jqkq5ag.jpg
20XX-XX-XX 21:16:32,128 - src.predictor - INFO - Processing image: /dev/landingai-greengrass-samples/components/landinglense.samples.LandingAi.Inference/tests/model/images/test.5jqkq5ag.jpg
20XX-XX-XX 21:16:33,042 - landingai.timer - INFO - Timer 'EdgePredictor.predict' finished. Elapsed time: 0.734 seconds.
20XX-XX-XX 21:16:33,042 - src.predictor - INFO - Processing inference results
20XX-XX-XX 21:16:33,042 - src.predictor - INFO - Processing list of predictions
20XX-XX-XX 21:16:33,042 - src.predictor - INFO - Converting ClassificationPrediction to dictionary
20XX-XX-XX 21:16:33,042 - src.predictor - INFO - Publishing results to device/test-device/inference_results
20XX-XX-XX 21:16:33,042 - src.mock_mqtt - INFO - Mock MQTT: Publishing to device/test-device/inference_results: {"device_id": "test-device", "inference_results": {"predictions": [{"score": 0.9987832903862, "label_name": "pushback", "label_index": 2}]}, "inference_time_ms": 913.2170677185059, "inference_engine_host": "localhost", "inference_engine_port": 8000, "timestamp": "20XX-05-20T04:16:33.042248+00:00"}
20XX-XX-XX 21:16:33,042 - __main__ - INFO - Received inference results: {"device_id": "test-device", "inference_results": {"predictions": [{"score": 0.9987832903862, "label_name": "pushback", "label_index": 2}]}, "inference_time_ms": 913.2170677185059, "inference_engine_host": "localhost", "inference_engine_port": 8000, "timestamp": "20XX-05-20T04:16:33.042248+00:00"}
Waiting for inference results...

Inference results received:
Device ID: test-device
Inference Engine: None:None
Inference Time: 913.2170677185059 ms

Model predictions:
  predictions: [{'score': 0.9987832903862, 'label_name': 'pushback', 'label_index': 2}]
command> 
```

### Quick Start
1. Import the necessary components:
```python
from src.mock_mqtt import MockMQTTClient
from tests.test_predictor import TestClient
```

2. Create a test client:
```python
mqtt_client = MockMQTTClient()
test_client = TestClient(
    mqtt_client=mqtt_client,
    device_id="test-device-001",
    topic_prefix="predictor"
)
```

3. Connect and send commands:
```python
async def main():
    await test_client.connect()
    await test_client.send_image_command("path/to/image.jpg")
    results = await test_client.wait_for_inference_results()
    print(f"Inference results: {results}")
```

## Data Flow
The predictor implements a request-response pattern over MQTT for image classification tasks.

```ascii
Client                    MQTT Broker                 Predictor
  |                           |                          |
  |-- Command (image_path) -->|-- Command ------------->|
  |                           |                          |
  |<---- Status updates ------|<---- Status updates ----|
  |                           |                          |
  |<-- Inference results -----|<-- Inference results ---|
```

Key component interactions:
1. Client sends image processing commands to specific MQTT topics
2. Predictor service monitors command topics for new requests
3. Status updates are published during processing phases
4. Inference results are published upon completion
5. All communication is asynchronous and non-blocking
6. Results include timing information and inference details
7. Error handling is implemented through status messages