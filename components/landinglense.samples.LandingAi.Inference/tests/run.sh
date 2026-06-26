#!/bin/bash
# Script to run Docker/Podman Compose with environment variables from .env file

# Configuration
COMPOSE_FILE="docker-compose-local.yaml"
REQUIRED_VARS=("LOCAL_MODEL_ID" "MODEL_VOLUME_PATH")

# Display help
show_help() {
  echo "Usage: $0 [--down]"
  echo "Options:"
  echo "  -h, --help     Show this help message"
  echo "  --down         Bring down the services instead of up"
  echo ""
  echo "This script reads configuration from the .env file."
  echo "Copy .env.template to .env and modify the values as needed."
  echo "Using Docker Compose file: $COMPOSE_FILE"
  echo ""
  echo "Example usage:"
  echo "  $0           # Start the service using configuration from .env"
  echo "  $0 --down    # Stop the service"
}

# Load environment variables from .env file
load_env_vars() {
  local env_file="$1"
  
  echo "Loading environment variables from $env_file file"
  
  # Initialize tracked variables to empty
  for var in "${REQUIRED_VARS[@]}"; do
    eval "$var=''"
  done
  
  # Read the .env file line by line
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip comments and empty lines
    [[ $line =~ ^#.*$ || -z $line ]] && continue
    
    # Extract variable name and value
    if [[ $line =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      var_name="${BASH_REMATCH[1]}"
      var_value="${BASH_REMATCH[2]}"
      
      # Remove quotes if present
      var_value="${var_value%\"}"
      var_value="${var_value#\"}"
      var_value="${var_value%\'}"
      var_value="${var_value#\'}"
      
      # Export the variable for use in this script
      export "$var_name=$var_value"
    fi
  done < "$env_file"
}

# Validate required environment variables
validate_env_vars() {
  local missing_vars=()
  
  for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
      missing_vars+=("$var")
    fi
  done
  
  if [ ${#missing_vars[@]} -gt 0 ]; then
    echo "Error: The following required variables are missing in .env file:"
    for var in "${missing_vars[@]}"; do
      echo "  - $var"
    done
    exit 1
  fi
}

# Display configuration summary
show_config() {
  echo "Configuration:"
  echo "  Command: run-local-model --model /data/model/bundle_${LOCAL_MODEL_ID}.zip ${ADDITIONAL_ARGS}"
  echo "  Container: ${CONTAINER_NAME:-ll_inference}"
  echo "  Network: ${NETWORK_MODE:-bridge}"
  
  if [ "${NETWORK_MODE:-bridge}" != "host" ]; then
    echo "  Port: ${PORT:-8000}"
  fi
  
  echo "  Volume: ${MODEL_VOLUME_PATH} -> /data/model"
}

# Run the compose command
run_compose() {
  local down="$1"
  local compose_file="$2"
  
  if [ "$down" = true ]; then
    echo "Stopping services..."
    podman compose -f "$compose_file" down
  else
    echo "Starting services..."
    echo "Using command: run-local-model --model /data/model/bundle_${LOCAL_MODEL_ID}.zip ${ADDITIONAL_ARGS}"
    # Use system environment variables directly
    # podman compose -f "$compose_file" config
    podman compose -f "$compose_file" up
  fi
}

# Main function
main() {
  # Parse command-line arguments
  local down=false
  
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)
        show_help
        exit 0
        ;;
      --down)
        down=true
        shift
        ;;
      *)
        echo "Unknown option: $1"
        show_help
        exit 1
        ;;
    esac
  done
  
  # Check if Docker Compose file exists
  if [ ! -f "$COMPOSE_FILE" ]; then
    echo "Error: Docker Compose file '$COMPOSE_FILE' not found"
    exit 1
  fi
  
  # Check if .env file exists
  if [ ! -f .env ]; then
    echo "Error: .env file not found"
    echo "Please copy .env.template to .env and modify it:"
    echo "  cp .env.template .env"
    exit 1
  fi
  
  # Load and validate environment variables
  load_env_vars ".env"
  validate_env_vars
  
  # Show configuration
  show_config
  
  # Run Docker Compose
  run_compose "$down" "$COMPOSE_FILE"
}

# Execute main function
main "$@"