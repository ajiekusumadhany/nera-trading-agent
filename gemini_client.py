import os
import logging
from config import GEMINI_API_KEY
from google import genai
from PIL import Image

logger = logging.getLogger(__name__)

# Initialize client
try:
    client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.error(f"Failed to initialize Gemini Client: {e}")
    client = None

def ask_gemini_text(prompt: str, context: str = "") -> str:
    """Send text and context to Gemini 2.5 Pro for analysis."""
    if not client:
        return "ERROR: Gemini Client not initialized."
    try:
        full_prompt = f"Context: {context}\n\nTask: {prompt}" if context else prompt
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=full_prompt,
        )
        return response.text
    except Exception as e:
        logger.error(f"Gemini text error: {e}")
        return f"ERROR: {e}"

def ask_gemini_vision(prompt: str, image_path: str) -> str:
    """Send an image and prompt to Gemini 2.5 Pro for visual analysis."""
    if not client:
        return "ERROR: Gemini Client not initialized."
    try:
        if not os.path.exists(image_path):
            return f"ERROR: Image not found at {image_path}"
            
        img = Image.open(image_path)
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=[prompt, img]
        )
        return response.text
    except Exception as e:
        logger.error(f"Gemini vision error: {e}")
        return f"ERROR: {e}"
