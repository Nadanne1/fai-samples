import logging
import json
import os
import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Union
from landingai.predict import EdgePredictor
import PIL.Image

from src.utils.logging_config import get_logger
from .mqtt_interface import MQTTInterface

logger = get_logger(__name__)

class Predictor:
    """
    Handles image classification operations and result processing.
    """
    def __init__(self, device_id: str, topic_prefix: str, inference_host: str, inference_port: str, mqtt_client: MQTTInterface):
        """
        Initialize the Predictor with environment configurations.
        
        Args:
            device_id: Unique identifier for this device
            topic_prefix: Prefix for MQTT topics
            inference_host: Host address for the inference engine
            inference_port: Port for the inference engine
            mqtt_client: An initialized MQTT client implementing MQTTInterface
        """
        
        # Load environment variables
        self.device_id = device_id
        self.topic_prefix = topic_prefix
        self.inference_host = inference_host
        self.inference_port = inference_port

        self.command_topic = f"{self.topic_prefix}/{self.device_id}/commands"
        self.inference_results_topic = f"{self.topic_prefix}/{self.device_id}/inference_results"
        self.status_topic = f"{self.topic_prefix}/{self.device_id}/status"

        logger.info("Initializing Image Classification Service")
        logger.debug(f"Device ID: {self.device_id}")
        logger.debug(f"Topic Prefix: {self.topic_prefix}")
        logger.debug(f"Inference Engine Host: {self.inference_host}")
        logger.debug(f"Inference Engine Port: {self.inference_port}")
        
        # Initialize components
        logger.info(f"Initializing EdgePredictor: {self.inference_host}:{self.inference_port}")
        self.predictor = EdgePredictor(host=self.inference_host, port=self.inference_port)
        
        # Store the MQTT client
        self.mqtt_client = mqtt_client
        self._running = False

    def get_utc_timestamp(self) -> str:
        """
        Get current UTC timestamp in ISO format.
        
        Returns:
            str: ISO formatted UTC timestamp
        """
        return datetime.now(timezone.utc).isoformat()
    
    def _serialize_prediction(self, prediction):
        """
        Serialize prediction objects into JSON-serializable dictionaries.
        
        This handles different types of prediction objects from LandingAI.
        
        Args:
            prediction: A prediction object from LandingAI
            
        Returns:
            dict: A JSON-serializable representation of the prediction
        """
        try:
            # Check if it's already a primitive type that's JSON serializable
            json.dumps(prediction)
            return prediction
        except TypeError:
            # It's a custom object, so we need to handle it specifically
            
            # Handle ClassificationPrediction objects 
            if hasattr(prediction, '__dict__'):
                logger.info(f"Converting {prediction.__class__.__name__} to dictionary")
                # Try to access common attributes for classification
                result = {}
                
                # Try to get common attributes
                if hasattr(prediction, 'class_name'):
                    result['class_name'] = prediction.class_name
                if hasattr(prediction, 'confidence'):
                    result['confidence'] = prediction.confidence
                if hasattr(prediction, 'class_id'):
                    result['class_id'] = prediction.class_id
                
                # If it doesn't have standard attributes, try to convert the entire __dict__
                if not result and hasattr(prediction, '__dict__'):
                    # Filter out private attributes (those starting with _)
                    result = {k: v for k, v in prediction.__dict__.items() 
                              if not k.startswith('_')}
                    
                return result
                
            # Handle other object types by converting to string
            logger.warning(f"Unhandled prediction type: {type(prediction)}, converting to string")
            return str(prediction)
        
    async def handle_results(self, results: Union[Dict[str, Any], List[Any]], inference_time: float) -> None:
        """
        Process and publish inference results.
        
        Args:
            results: Inference results (either dictionary or list)
            inference_time: Time taken for inference in milliseconds
        """
        try:
            logger.info("Processing inference results")
            logger.debug(f"Raw results type: {type(results)}")
            
            # Serialize the results to make them JSON-compatible
            if isinstance(results, list):
                logger.info("Processing list of predictions")
                formatted_results = {
                    "predictions": [self._serialize_prediction(item) for item in results]
                }
            elif isinstance(results, dict):
                logger.info("Processing dictionary of predictions")
                formatted_results = {}
                for key, value in results.items():
                    if isinstance(value, list):
                        formatted_results[key] = [self._serialize_prediction(item) for item in value]
                    else:
                        formatted_results[key] = self._serialize_prediction(value)
            else:
                logger.warning(f"Unexpected result type: {type(results)}, converting to string")
                formatted_results = {"raw_result": str(results)}

            # Prepare message payload
            message = {
                "device_id": self.device_id,
                "inference_results": formatted_results,
                "inference_time_ms": inference_time,
                "inference_engine_host": self.inference_host,
                "inference_engine_port": self.inference_port,
                "timestamp": self.get_utc_timestamp()
            }
            
            # Verify JSON serialization works before publishing
            try:
                json_str = json.dumps(message)
                logger.debug(f"Serialized message length: {len(json_str)} bytes")
            except TypeError as e:
                logger.warning(f"JSON serialization error: {e}")
                # Attempt more aggressive serialization
                message["inference_results"] = str(formatted_results)
                
            # Publish results
            logger.info(f"Publishing results to {self.inference_results_topic}")
            await self.mqtt_client.publish(self.inference_results_topic, message)
            
        except Exception as e:
            logger.warning(f"Error handling results: {e}")

    async def process_image_message(self, topic: str, message: Dict[str, Any]) -> None:
        """
        Process incoming image message
        
        Args:
            topic: The topic the message was received on
            message: Message data containing image path
        """
        try:
            logger.info(f"Processing message on topic {topic}: {message}")
            
            # Extract image path from message
            image_path = message.get("image_path")
            logger.info(f"Received request to process image: {image_path}")
            
            # Validate image path
            if not image_path:
                error_msg = "No image path in message"
                logger.error(error_msg)
                await self.publish_status(error_msg)
                return
                
            if not os.path.exists(image_path):
                error_msg = f"Image file not found: {image_path}"
                logger.error(error_msg)
                await self.publish_status(error_msg)
                return
            
            # Process image
            logger.info(f"Processing image: {image_path}")
            
            start_time = time.time()
            img = PIL.Image.open(image_path)

            try:
                # Try to get predictions
                predictions = self.predictor.predict(img)
                inference_time = (time.time() - start_time) * 1000  # Convert to ms
                
                await self.handle_results(predictions, inference_time)
                
            except Exception as inference_error:
                logger.warning(f"Inference failed: {str(inference_error)}")
                await self.publish_status(f"Warning: Inference failed - {str(inference_error)}")
                
                # Try to reconnect to inference service if needed
                try:
                    logger.info("Attempting to reconnect to inference service...")
                    # Reinitialize the predictor
                    self.predictor = EdgePredictor(host=self.inference_host, port=self.inference_port)
                    logger.info("Successfully reconnected to inference service")
                except Exception as reconnect_error:
                    logger.warning(f"Failed to reconnect: {str(reconnect_error)}")

        except Exception as e:
            logger.warning(f"Error processing image: {str(e)}")
            await self.publish_status(f"Error: {str(e)}")

    async def publish_status(self, status_message: str) -> None:
        """
        Publish a status message to IoT Core.

        Args:
            status_message: Status message to publish
        """
        status_data = {
            'status': status_message,
            'timestamp': self.get_utc_timestamp(),
            'deviceId': self.device_id
        }
        await self.mqtt_client.publish(self.status_topic, status_data)

    async def start(self) -> None:
        """
        Start the predictor service asynchronously.
        
        This method initializes the service and sets up message handling.
        """
        try:
            logger.info("Starting Image Classification Service...")
            self._running = True  # Set running flag
            
            # Connect to MQTT
            success = await self.mqtt_client.connect()
            if not success:
                raise ConnectionError("Failed to connect to MQTT")
            
            # Subscribe to command topic
            success = await self.mqtt_client.subscribe(self.command_topic, self.process_image_message)
            if not success:
                raise ConnectionError(f"Failed to subscribe to {self.command_topic}")
            
            # Report ready status
            await self.publish_status("READY")
            
            logger.info("Service started successfully")
            
        except Exception as e:
            logger.warning(f"Error starting service: {str(e)}")
            raise

    async def stop(self) -> None:
        """
        Stop the predictor service.
        
        This method performs cleanup when the component is being stopped.
        """
        try:
            logger.info("Stopping Edge Predictor Service...")
            await self.publish_status("STOPPING")
            
            # Unsubscribe from command topic
            if self._running:
                await self.mqtt_client.unsubscribe(self.command_topic)
            
            # Disconnect from MQTT
            await self.mqtt_client.disconnect()
            
            self._running = False
            logger.info("Service stopped successfully")
            
        except Exception as e:
            logger.warning(f"Error stopping service: {str(e)}")
            raise