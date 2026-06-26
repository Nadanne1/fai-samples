import os
import sys
import argparse
import logging
import asyncio
import signal
import json
from datetime import datetime
import time
from typing import Dict, Any, Optional

# Add parent directory to path to allow importing from src
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.mock_mqtt import MockMQTTClient
from src.predictor import Predictor
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# Global variables for clean shutdown
predictor = None
shutdown_event = asyncio.Event()

def signal_handler(sig, frame):
    """Handle Ctrl+C signals"""
    logger.info("Shutdown signal received")
    shutdown_event.set()

class TestClient:
    """
    A test client that can be used to interact with the Predictor service
    """
    
    def __init__(self, mqtt_client, device_id: str, topic_prefix: str):
        """
        Initialize the test client
        
        Args:
            mqtt_client: An instance of MockMQTTClient
            device_id: Device ID of the predictor
            topic_prefix: Topic prefix used by the predictor
        """
        self.logger = logging.getLogger(__name__)
        self.mqtt_client = mqtt_client
        self.device_id = device_id
        self.topic_prefix = topic_prefix
        
        # Define topics
        self.command_topic = f"{topic_prefix}/{device_id}/commands"
        self.status_topic = f"{topic_prefix}/{device_id}/status"
        self.inference_results_topic = f"{topic_prefix}/{device_id}/inference_results"
        
        # Store received messages
        self.status_messages = []
        self.inference_results = []
        
    async def connect(self):
        """Connect to the MQTT broker and subscribe to response topics"""
        await self.mqtt_client.connect()
        
        # Subscribe to status and results topics
        await self.mqtt_client.subscribe(self.status_topic, self._on_status_message)
        await self.mqtt_client.subscribe(self.inference_results_topic, self._on_inference_results)
        
        self.logger.info(f"Test client connected and subscribed to response topics")
    
    async def _on_status_message(self, topic: str, payload: Dict[str, Any]):
        """Handle incoming status messages"""
        self.logger.info(f"Received status: {json.dumps(payload)}")
        self.status_messages.append(payload)
    
    async def _on_inference_results(self, topic: str, payload: Dict[str, Any]):
        """Handle incoming inference results"""
        self.logger.info(f"Received inference results: {json.dumps(payload)}")
        self.inference_results.append(payload)
    
    async def send_image_command(self, image_path: str):
        """
        Send a command to process an image
        
        Args:
            image_path: Path to the image file
        """
        command = {
            "image_path": image_path,
            "timestamp": time.time()
        }
        
        self.logger.info(f"Sending command to process image: {image_path}")
        await self.mqtt_client.publish(self.command_topic, command)
    
    async def wait_for_status(self, expected_status: str, timeout: float = 5.0) -> bool:
        """
        Wait for a specific status message
        
        Args:
            expected_status: The status string to wait for
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if the status was received, False if timed out
        """
        start_time = time.time()
        
        # Check if we already received the status
        for msg in self.status_messages:
            if msg.get('status') == expected_status:
                return True
        
        # Wait for the status
        while time.time() - start_time < timeout:
            await asyncio.sleep(0.1)
            
            # Check latest status messages
            for msg in self.status_messages:
                if msg.get('status') == expected_status:
                    return True
        
        self.logger.warning(f"Timed out waiting for status: {expected_status}")
        return False
    
    async def wait_for_inference_results(self, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """
        Wait for inference results
        
        Args:
            timeout: Maximum time to wait in seconds
            
        Returns:
            The inference results dictionary or None if timed out
        """
        start_time = time.time()
        
        # Check if we already received results
        if self.inference_results:
            return self.inference_results[-1]
        
        # Wait for results
        while time.time() - start_time < timeout:
            await asyncio.sleep(0.1)
            
            if self.inference_results:
                return self.inference_results[-1]
        
        self.logger.warning("Timed out waiting for inference results")
        return None
    
    async def disconnect(self):
        """Disconnect from the MQTT broker"""
        await self.mqtt_client.unsubscribe(self.status_topic)
        await self.mqtt_client.unsubscribe(self.inference_results_topic)
        await self.mqtt_client.disconnect()
        self.logger.info("Test client disconnected")

def print_help():
    """Print help information"""
    print("\nAvailable Commands:")
    print("  help                                   - Show this help message")
    print("  status                                 - Show current status")
    print("  process <image_path>                   - Process an image")
    print("  exit/quit                              - Exit the program")
    print()

async def run_cli_interface(test_client: TestClient):
    """
    Run a CLI interface for interacting with the predictor service
    
    Args:
        test_client: The test client to use for communication
    """
    print("\nImage Classification Test CLI")
    print("============================")
    print("Type 'help' for available commands")
    
    input_task = None
    
    while not shutdown_event.is_set():
        try:
            # Create input task if it doesn't exist
            if input_task is None:
                input_task = asyncio.create_task(asyncio.to_thread(input, "command> "))
            
            # Wait for input with a timeout to allow checking shutdown event
            done, pending = await asyncio.wait(
                [input_task], 
                timeout=0.1,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Check if we need to exit
            if shutdown_event.is_set():
                if pending:
                    for task in pending:
                        task.cancel()
                break
            
            # Process input if ready
            if done:
                input_task = None  # Reset for next input
                user_input = done.pop().result().strip()
                
                if not user_input:
                    continue
                
                # Handle exit command
                if user_input.lower() in ['exit', 'quit']:
                    print("Exiting...")
                    shutdown_event.set()
                    break
                
                # Handle other commands
                elif user_input.lower() == 'help':
                    print_help()
                
                elif user_input.lower() == 'status':
                    print("Checking status...")
                    # Check if predictor is READY
                    is_ready = await test_client.wait_for_status("READY", timeout=1.0)
                    
                    if is_ready:
                        print("Predictor status: READY")
                    else:
                        print("Predictor status: NOT READY")
                        
                    # Show recent status messages
                    if test_client.status_messages:
                        print("\nRecent status messages:")
                        for i, msg in enumerate(test_client.status_messages[-5:]):
                            print(f"  {i+1}. Status: {msg.get('status', 'Unknown')}")
                            print(f"     Timestamp: {msg.get('timestamp', 'Unknown')}")
                            print(f"     Device ID: {msg.get('deviceId', 'Unknown')}")
                            print()
                
                elif user_input.lower().startswith('process'):
                    # Parse process command
                    parts = user_input.split()
                    if len(parts) < 2:
                        print("Usage: process <image_path>")
                        continue
                    
                    image_path = parts[1]
                    
                    # Validate image path
                    if not os.path.exists(image_path):
                        print(f"Error: Image file not found: {image_path}")
                        continue
                    
                    print(f"Sending command to process image: {image_path}")
                    await test_client.send_image_command(image_path)
                    
                    # Wait for results
                    print("Waiting for inference results...")
                    results = await test_client.wait_for_inference_results(timeout=10.0)
                    
                    if results:
                        print("\nInference results received:")
                        print(f"Device ID: {results.get('device_id')}")
                        print(f"Inference Engine: {results.get('Inference Engine Host')}:{results.get('Inference Engine Port')}")
                        print(f"Inference Time: {results.get('inference_time_ms')} ms")
                        
                        # Print inference results in a prettier format
                        inference_results = results.get('inference_results', {})
                        if inference_results:
                            print("\nModel predictions:")
                            if isinstance(inference_results, dict):
                                for key, value in inference_results.items():
                                    print(f"  {key}: {value}")
                            else:
                                print(f"  {inference_results}")
                        else:
                            print("No inference results data received")
                    else:
                        print("Did not receive inference results in time")
                
                else:
                    print(f"Unknown command: {user_input}")
                    print("Type 'help' for available commands")
                    
        except asyncio.CancelledError:
            logger.info("CLI interface task cancelled")
            break
        except Exception as e:
            print(f"Error in CLI interface: {e}")
            logger.exception("CLI interface error")
            # Reset input task on error
            input_task = None
    
    # Clean up any pending input task
    if input_task and not input_task.done():
        input_task.cancel()
        try:
            await input_task
        except (asyncio.CancelledError, Exception):
            pass
            
    logger.info("CLI interface stopped")

async def run_test(args):
    logger.info("Starting test with mock MQTT client")
    
    # Create the mock MQTT client
    mqtt_client = MockMQTTClient()
    
    # Create the test client
    test_client = TestClient(mqtt_client, args.device_id, args.topic_prefix)
    
    # Create and start the predictor
    global predictor
    predictor = Predictor(
        device_id=args.device_id,
        topic_prefix=args.topic_prefix,
        inference_host=args.inference_host,
        inference_port=int(args.inference_port),
        mqtt_client=mqtt_client
    )
    
    try:
        # Register signal handlers
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Start both clients
        await test_client.connect()
        await predictor.start()
        
        # Wait for the READY status
        logger.info("Waiting for READY status...")
        ready = await test_client.wait_for_status("READY", timeout=5.0)
        
        if not ready:
            logger.error("Predictor did not become ready in time")
            return
        
        logger.info("Predictor is ready")
        
        # Check if an image path was provided for automatic testing
        if args.image_path:
            logger.info(f"Testing with image: {args.image_path}")
            await test_client.send_image_command(args.image_path)
            
            # Wait for inference results
            results = await test_client.wait_for_inference_results(timeout=10.0)
            
            if results:
                logger.info("Received inference results:")
                logger.info(f"Device ID: {results.get('device_id')}")
                logger.info(f"Inference Engine: {results.get('Inference Engine Host')}:{results.get('Inference Engine Port')}")
                logger.info(f"Inference Time: {results.get('inference_time_ms')} ms")
                logger.info(f"Results: {results.get('inference_results')}")
                
                # If in auto mode, exit after processing
                if args.auto_exit:
                    logger.info("Auto exit enabled, shutting down...")
                    shutdown_event.set()
                    return
            else:
                logger.error("Did not receive inference results in time")
                if args.auto_exit:
                    shutdown_event.set()
                    return
        
        # Start the CLI if not in auto mode or if auto processing failed
        if not args.auto_exit or not args.image_path:
            # Start CLI interface
            cli_task = asyncio.create_task(run_cli_interface(test_client))
            
            # Wait for shutdown signal
            await shutdown_event.wait()
            
            # Cancel CLI task
            if cli_task:
                cli_task.cancel()
                try:
                    await cli_task
                except asyncio.CancelledError:
                    pass
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        logger.exception("Stack trace:")
    finally:
        # Cleanup
        if predictor:
            await predictor.stop()
        await test_client.disconnect()
        logger.info("Test completed and resources cleaned up")

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Test the Predictor with mock MQTT")
    parser.add_argument("--device-id", type=str, default="test-device", help="Device ID")
    parser.add_argument("--topic-prefix", type=str, default="device", help="Topic prefix")
    parser.add_argument("--inference-host", type=str, default="localhost", help="Inference engine host")
    parser.add_argument("--inference-port", type=str, default="8000", help="Inference engine port")
    parser.add_argument("--image-path", type=str, help="Path to a test image to process")
    parser.add_argument("--auto-exit", action="store_true", help="Exit after processing the image (only with --image-path)")
    
    args = parser.parse_args()
    
    try:
        # Run the test
        asyncio.run(run_test(args))
    except KeyboardInterrupt:
        print("\nTest interrupted")
    except Exception as e:
        logger.error(f"Unhandled exception: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()