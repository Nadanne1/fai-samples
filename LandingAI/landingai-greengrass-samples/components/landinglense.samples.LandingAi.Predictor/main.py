import sys
import os
import logging
import argparse
import asyncio

# Import the MQTT implementation
from src.greengrass_mqtt import GreengrassSDKClient
from src.predictor import Predictor
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

async def run_service(args):
    logger = logging.getLogger(__name__)
    logger.info("Starting Image Classification Service")
    
    try:
        # Initialize MQTT client for Greengrass
        logger.info("Using AWS IoT Greengrass MQTT client")
        mqtt_client = GreengrassSDKClient()
        
        # Initialize and start predictor
        predictor = Predictor(
            device_id=args.device_id,
            topic_prefix=args.topic_prefix,
            inference_host=args.inference_host,
            inference_port=int(args.inference_port),
            mqtt_client=mqtt_client
        )
        
        # Start the service
        await predictor.start()
        
        # Keep the service running
        while True:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Service interrupted, shutting down...")
        await predictor.stop()
    except Exception as e:
        logger.error(f"Application failed: {e}")
        logger.exception("Stack trace:")
        if 'predictor' in locals():
            await predictor.stop()
        sys.exit(1)

def main():    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Image Classification Service")
    logger.info(f"[Main] Arguments: {sys.argv[1:]}")
    parser.add_argument("--device-id", type=str, required=True, help='Device name')
    parser.add_argument("--topic-prefix", type=str, required=True, help='IoT Core topic prefix')
    parser.add_argument("--inference-host", type=str, required=True, help='IP or hostname of the inference engine')
    parser.add_argument("--inference-port", type=str, required=True, help='Service port of the inference engine')
    
    args = parser.parse_args()
    
    # Run the async service
    asyncio.run(run_service(args))

if __name__ == "__main__":
    main()