import sys
import os
from dotenv import load_dotenv

# Get the absolute path of the project root directory
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Add the project root to the Python path
sys.path.insert(0, project_root)

# Load environment variables from .env file
load_dotenv(os.path.join(project_root, '.env'))