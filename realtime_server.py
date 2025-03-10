import asyncio
import json
import os
from dotenv import load_dotenv
import numpy as np
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
import uvicorn
import logging
from prompts import PROMPTS
from openai_realtime_client import OpenAIRealtimeAudioTextClient
from starlette.websockets import WebSocketState
import wave
import datetime
import scipy.signal
from openai import OpenAI, AsyncOpenAI
from pydantic import BaseModel, Field
from typing import Generator
from llm_processor import get_llm_processor
from datetime import datetime, timedelta
import argparse
import pyperclip  # 添加剪贴板库

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# Pydantic models for request and response schemas
class ReadabilityRequest(BaseModel):
    text: str = Field(..., description="The text to improve readability for.")

class ReadabilityResponse(BaseModel):
    enhanced_text: str = Field(..., description="The text with improved readability.")

class CorrectnessRequest(BaseModel):
    text: str = Field(..., description="The text to check for factual correctness.")

class CorrectnessResponse(BaseModel):
    analysis: str = Field(..., description="The factual correctness analysis.")

class AskAIRequest(BaseModel):
    text: str = Field(..., description="The question to ask AI.")

class AskAIResponse(BaseModel):
    answer: str = Field(..., description="AI's answer to the question.")

class ClipboardRequest(BaseModel):
    text: str = Field(..., description="The text to copy to the computer's clipboard.")
    auto_paste: bool = Field(False, description="Whether to automatically paste the text after copying.")

class ClipboardResponse(BaseModel):
    success: bool = Field(..., description="Whether the operation was successful.")
    message: str = Field(..., description="A message describing the result of the operation.")

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY is not set in environment variables.")
    raise EnvironmentError("OPENAI_API_KEY is not set.")

# Initialize with a default model
llm_processor = get_llm_processor("gpt-4o")  # Default processor

# Use an absolute path for the static directory
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

def log_content(content_type, content):
    """
    记录内容到日志文件
    
    Args:
        content_type: 内容类型 ("Transcript", "Readability", "Correctness", "AskAI")
        content: 要记录的内容
    """
    # 获取当前日期作为文件名
    today = datetime.now().strftime("%Y-%m-%d")
    
    # 使用绝对路径，确保日志文件被创建在正确的位置
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base_dir, "logs")
    log_file = os.path.join(log_dir, f"{today}.log")
    
    # 确保日志目录存在
    os.makedirs(log_dir, exist_ok=True)
    
    # 格式化当前时间和内容
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_content = f"""
=== {content_type} - {timestamp} ===
{content}
==============================================
"""
    
    # 写入日志文件
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(formatted_content)
    
    logger.info(f"Logged {content_type} content to {log_file}")

@app.get("/", response_class=HTMLResponse)
async def get_realtime_page(request: Request):
    return FileResponse(os.path.join(os.path.dirname(__file__), "static/realtime.html"))

class AudioProcessor:
    def __init__(self, target_sample_rate=24000):
        self.target_sample_rate = target_sample_rate
        self.source_sample_rate = 48000  # Most common sample rate for microphones
        
    def process_audio_chunk(self, audio_data):
        # Convert binary audio data to Int16 array
        pcm_data = np.frombuffer(audio_data, dtype=np.int16)
        
        # Convert to float32 for better precision during resampling
        float_data = pcm_data.astype(np.float32) / 32768.0
        
        # Resample from 48kHz to 24kHz
        resampled_data = scipy.signal.resample_poly(
            float_data, 
            self.target_sample_rate, 
            self.source_sample_rate
        )
        
        # Convert back to int16 while preserving amplitude
        resampled_int16 = (resampled_data * 32768.0).clip(-32768, 32767).astype(np.int16)
        return resampled_int16.tobytes()

    def save_audio_buffer(self, audio_buffer, filename):
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(1)  # Mono audio
            wf.setsampwidth(2)  # 2 bytes per sample (16-bit)
            wf.setframerate(self.target_sample_rate)
            wf.writeframes(b''.join(audio_buffer))
        logger.info(f"Saved audio buffer to {filename}")

@app.websocket("/api/v1/ws")
async def websocket_endpoint(websocket: WebSocket):
    logger.info("New WebSocket connection attempt")
    await websocket.accept()
    logger.info("WebSocket connection accepted")
    
    # Add initial status update here
    await websocket.send_text(json.dumps({
        "type": "status",
        "status": "idle"  # Set initial status to idle (blue)
    }))
    
    client = None
    audio_processor = AudioProcessor()
    audio_buffer = []
    recording_stopped = asyncio.Event()
    openai_ready = asyncio.Event()
    pending_audio_chunks = []
    
    # 添加变量跟踪完整的听译内容
    full_transcript = ""
    
    async def initialize_openai():
        nonlocal client
        try:
            # Clear the ready flag while initializing
            openai_ready.clear()
            
            client = OpenAIRealtimeAudioTextClient(os.getenv("OPENAI_API_KEY"))
            await client.connect()
            logger.info("Successfully connected to OpenAI client")
            
            # Register handlers after client is initialized
            client.register_handler("session.updated", lambda data: handle_generic_event("session.updated", data))
            client.register_handler("input_audio_buffer.cleared", lambda data: handle_generic_event("input_audio_buffer.cleared", data))
            client.register_handler("input_audio_buffer.speech_started", lambda data: handle_generic_event("input_audio_buffer.speech_started", data))
            client.register_handler("rate_limits.updated", lambda data: handle_generic_event("rate_limits.updated", data))
            client.register_handler("response.output_item.added", lambda data: handle_generic_event("response.output_item.added", data))
            client.register_handler("conversation.item.created", lambda data: handle_generic_event("conversation.item.created", data))
            client.register_handler("response.content_part.added", lambda data: handle_generic_event("response.content_part.added", data))
            client.register_handler("response.text.done", lambda data: handle_generic_event("response.text.done", data))
            client.register_handler("response.content_part.done", lambda data: handle_generic_event("response.content_part.done", data))
            client.register_handler("response.output_item.done", lambda data: handle_generic_event("response.output_item.done", data))
            client.register_handler("response.done", lambda data: handle_response_done(data))
            client.register_handler("error", lambda data: handle_error(data))
            client.register_handler("response.text.delta", lambda data: handle_text_delta(data))
            client.register_handler("response.created", lambda data: handle_response_created(data))
            
            openai_ready.set()  # Set ready flag after successful initialization
            await websocket.send_text(json.dumps({
                "type": "status",
                "status": "connected"
            }))
            return True
        except Exception as e:
            logger.error(f"Failed to connect to OpenAI: {e}")
            openai_ready.clear()  # Ensure flag is cleared on failure
            await websocket.send_text(json.dumps({
                "type": "error",
                "content": "Failed to initialize OpenAI connection"
            }))
            return False

    # Move the handler definitions here (before initialize_openai)
    async def handle_text_delta(data):
        nonlocal full_transcript
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                delta = data.get("delta", "")
                full_transcript += delta  # 累积完整的听译内容
                await websocket.send_text(json.dumps({
                    "type": "text",
                    "content": delta,
                    "isNewResponse": False
                }))
                logger.info("Handled response.text.delta")
        except Exception as e:
            logger.error(f"Error in handle_text_delta: {str(e)}", exc_info=True)

    async def handle_response_created(data):
        nonlocal full_transcript
        full_transcript = ""  # 重置完整的听译内容
        await websocket.send_text(json.dumps({
            "type": "text",
            "content": "",
            "isNewResponse": True
        }))
        logger.info("Handled response.created")

    async def handle_error(data):
        error_msg = data.get("error", {}).get("message", "Unknown error")
        logger.error(f"OpenAI error: {error_msg}")
        await websocket.send_text(json.dumps({
            "type": "error",
            "content": error_msg
        }))
        logger.info("Handled error message from OpenAI")

    async def handle_response_done(data):
        nonlocal client, full_transcript
        logger.info("Handled response.done")
        recording_stopped.set()
        
        # 记录完整的听译内容到日志
        if full_transcript:
            log_content("Transcript", full_transcript)
        
        if client:
            try:
                await client.close()
                client = None
                openai_ready.clear()
                await websocket.send_text(json.dumps({
                    "type": "status",
                    "status": "idle"
                }))
                logger.info("Connection closed after response completion")
            except Exception as e:
                logger.error(f"Error closing client after response done: {str(e)}")

    async def handle_generic_event(event_type, data):
        logger.info(f"Handled {event_type} with data: {json.dumps(data, ensure_ascii=False)}")

    # Create a queue to handle incoming audio chunks
    audio_queue = asyncio.Queue()

    async def receive_messages():
        nonlocal client
        
        try:
            while True:
                if websocket.client_state == WebSocketState.DISCONNECTED:
                    logger.info("WebSocket client disconnected")
                    openai_ready.clear()
                    break
                    
                try:
                    # Add timeout to prevent infinite waiting
                    data = await asyncio.wait_for(websocket.receive(), timeout=30.0)
                    
                    if "bytes" in data:
                        processed_audio = audio_processor.process_audio_chunk(data["bytes"])
                        if not openai_ready.is_set():
                            logger.debug("OpenAI not ready, buffering audio chunk")
                            pending_audio_chunks.append(processed_audio)
                        elif client:
                            await client.send_audio(processed_audio)
                            await websocket.send_text(json.dumps({
                                "type": "status",
                                "status": "connected"
                            }))
                            logger.debug(f"Sent audio chunk, size: {len(processed_audio)} bytes")
                        else:
                            logger.warning("Received audio but client is not initialized")
                            
                    elif "text" in data:
                        msg = json.loads(data["text"])
                        
                        if msg.get("type") == "start_recording":
                            # Update status to connecting while initializing OpenAI
                            await websocket.send_text(json.dumps({
                                "type": "status",
                                "status": "connecting"
                            }))
                            if not await initialize_openai():
                                continue
                            recording_stopped.clear()
                            pending_audio_chunks.clear()
                            
                            # Send any buffered chunks
                            if pending_audio_chunks and client:
                                logger.info(f"Sending {len(pending_audio_chunks)} buffered chunks")
                                for chunk in pending_audio_chunks:
                                    await client.send_audio(chunk)
                                pending_audio_chunks.clear()
                            
                        elif msg.get("type") == "stop_recording":
                            if client:
                                await client.commit_audio()
                                await client.start_response(PROMPTS['paraphrase-gpt-realtime'])
                                await recording_stopped.wait()
                                # Don't close the client here, let the disconnect timer handle it
                                # Update client status to connected (waiting for response)
                                await websocket.send_text(json.dumps({
                                    "type": "status",
                                    "status": "connected"
                                }))

                except asyncio.TimeoutError:
                    logger.debug("No message received for 30 seconds")
                    continue
                except Exception as e:
                    logger.error(f"Error in receive_messages loop: {str(e)}", exc_info=True)
                    break
                
        finally:
            # Cleanup when the loop exits
            if client:
                try:
                    await client.close()
                except Exception as e:
                    logger.error(f"Error closing client in receive_messages: {str(e)}")
            logger.info("Receive messages loop ended")

    async def send_audio_messages():
        while True:
            try:
                processed_audio = await audio_queue.get()
                if processed_audio is None:
                    break
                
                # Add validation
                if len(processed_audio) == 0:
                    logger.warning("Empty audio chunk received, skipping")
                    continue
                
                # Append the processed audio to the buffer
                audio_buffer.append(processed_audio)

                await client.send_audio(processed_audio)
                logger.info(f"Audio chunk sent to OpenAI client, size: {len(processed_audio)} bytes")
                
            except Exception as e:
                logger.error(f"Error in send_audio_messages: {str(e)}", exc_info=True)
                break

        # After processing all audio, set the event
        recording_stopped.set()

    # Start concurrent tasks for receiving and sending
    receive_task = asyncio.create_task(receive_messages())
    send_task = asyncio.create_task(send_audio_messages())

    try:
        # Wait for both tasks to complete
        await asyncio.gather(receive_task, send_task)
    finally:
        if client:
            await client.close()
            logger.info("OpenAI client connection closed")

@app.post(
    "/api/v1/readability",
    response_model=ReadabilityResponse,
    summary="Enhance Text Readability",
    description="Improve the readability of the provided text using GPT-4."
)
async def enhance_readability(request: ReadabilityRequest):
    prompt = PROMPTS.get('readability-enhance')
    if not prompt:
        raise HTTPException(status_code=500, detail="Readability prompt not found.")

    try:
        # 用于收集完整的增强文本
        full_enhanced_text = ""
        
        async def text_generator():
            nonlocal full_enhanced_text
            # Use gpt-4o specifically for readability
            async for part in llm_processor.process_text(request.text, prompt, model="gpt-4o"):
                full_enhanced_text += part
                yield part
            
            # 在生成完整内容后记录到日志
            log_content("Readability", full_enhanced_text)

        return StreamingResponse(text_generator(), media_type="text/plain")

    except Exception as e:
        logger.error(f"Error enhancing readability: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error processing readability enhancement.")

@app.post(
    "/api/v1/ask_ai",
    response_model=AskAIResponse,
    summary="Ask AI a Question",
    description="Ask AI to provide insights using O1-mini model."
)
def ask_ai(request: AskAIRequest):
    prompt = PROMPTS.get('ask-ai')
    if not prompt:
        raise HTTPException(status_code=500, detail="Ask AI prompt not found.")

    try:
        # Use o1-mini specifically for ask_ai
        answer = llm_processor.process_text_sync(request.text, prompt, model="o1-mini")
        
        # 记录AI回答到日志
        log_content("AskAI", answer)
        
        return AskAIResponse(answer=answer)
    except Exception as e:
        logger.error(f"Error processing AI question: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error processing AI question.")

@app.post(
    "/api/v1/correctness",
    response_model=CorrectnessResponse,
    summary="Check Factual Correctness",
    description="Analyze the text for factual accuracy using GPT-4o."
)
async def check_correctness(request: CorrectnessRequest):
    prompt = PROMPTS.get('correctness-check')
    if not prompt:
        raise HTTPException(status_code=500, detail="Correctness prompt not found.")

    try:
        # 用于收集完整的正确性检查结果
        full_correctness_result = ""
        
        async def text_generator():
            nonlocal full_correctness_result
            # Specifically use gpt-4o for correctness checking
            async for part in llm_processor.process_text(request.text, prompt, model="gpt-4o"):
                full_correctness_result += part
                yield part
            
            # 在生成完整内容后记录到日志
            log_content("Correctness", full_correctness_result)

        return StreamingResponse(text_generator(), media_type="text/plain")

    except Exception as e:
        logger.error(f"Error checking correctness: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error processing correctness check.")

@app.post(
    "/api/v1/to_clipboard",
    response_model=ClipboardResponse,
    summary="Copy Text to Computer Clipboard",
    description="Copies the provided text to the computer's clipboard."
)
async def copy_to_clipboard(request: ClipboardRequest):
    try:
        # 在文本末尾添加两个换行符
        text_with_line_breaks = request.text + "\n\n"
        
        # 将文本复制到电脑剪贴板
        pyperclip.copy(text_with_line_breaks)
        
        # 如果启用了自动粘贴，则模拟Ctrl+V
        if request.auto_paste:
            try:
                import pyautogui
                # 等待一小段时间，确保剪贴板内容已更新
                await asyncio.sleep(0.5)
                # 模拟Ctrl+V粘贴操作
                pyautogui.hotkey('ctrl', 'v')
                return ClipboardResponse(
                    success=True,
                    message="Text successfully copied to computer clipboard and pasted."
                )
            except Exception as paste_error:
                logger.error(f"Error auto-pasting: {paste_error}", exc_info=True)
                return ClipboardResponse(
                    success=True,
                    message="Text copied to clipboard, but auto-paste failed."
                )
        
        return ClipboardResponse(
            success=True,
            message="Text successfully copied to computer clipboard."
        )
    except Exception as e:
        logger.error(f"Error copying to clipboard: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error copying to clipboard: {str(e)}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run the Brainwave realtime server')
    parser.add_argument('--ssl-certfile', help='Path to SSL certificate file for HTTPS')
    parser.add_argument('--port', type=int, default=3005, help='Port to run the server on')
    parser.add_argument('--host', default="0.0.0.0", help='Host to run the server on')
    
    args = parser.parse_args()
    
    # 确保logs目录存在（使用绝对路径）
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logger.info(f"Ensured logs directory exists at {log_dir}")
    
    if args.ssl_certfile:
        print(f"Running with HTTPS on {args.host}:{args.port}")
        uvicorn.run(app, host=args.host, port=args.port, ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_certfile)
    else:
        print(f"Running with HTTP on {args.host}:{args.port}")
        print("Note: Microphone access on mobile devices typically requires HTTPS.")
        uvicorn.run(app, host=args.host, port=args.port)
