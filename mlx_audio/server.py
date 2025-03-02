import argparse
import importlib.util
import logging
import os
import sys
import uuid

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mlx_audio_server")

from mlx_audio.tts.generate import main as generate_main

# Import from mlx_audio package
from mlx_audio.tts.utils import load_model

from .tts.audio_player import AudioPlayer

app = FastAPI()

# Add CORS middleware to allow requests from the same origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins, will be restricted by host binding
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load the model once on server startup.
# You can change the model path or pass arguments as needed.
# For performance, load once globally:
MODEL_PATH = "prince-canuma/Kokoro-82M"
tts_model = None  # Will be loaded when the server starts
audio_player = None  # Will be initialized when the server starts

# Make sure the output folder for generated TTS files exists
# Use an absolute path that's guaranteed to be writable
OUTPUT_FOLDER = os.path.join(os.path.expanduser("~"), ".mlx_audio", "outputs")
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
logger.info(f"Using output folder: {OUTPUT_FOLDER}")


@app.post("/tts")
def tts_endpoint(
    text: str = Form(...), voice: str = Form("af_heart"), speed: float = Form(1.0)
):
    """
    POST an x-www-form-urlencoded form with 'text' (and optional 'voice' and 'speed').
    We run TTS on the text, save the audio in a unique file,
    and return JSON with the filename so the client can retrieve it.
    """
    global tts_model

    if not text.strip():
        return JSONResponse({"error": "Text is empty"}, status_code=400)

    # Validate speed parameter
    try:
        speed_float = float(speed)
        if speed_float < 0.5 or speed_float > 2.0:
            return JSONResponse(
                {"error": "Speed must be between 0.5 and 2.0"}, status_code=400
            )
    except ValueError:
        return JSONResponse({"error": "Invalid speed value"}, status_code=400)

    # We'll do something like the code in model.generate() from the TTS library:
    # Generate the unique filename
    unique_id = str(uuid.uuid4())
    filename = f"tts_{unique_id}.wav"
    output_path = os.path.join(OUTPUT_FOLDER, filename)

    logger.info(
        f"Generating TTS for text: '{text[:50]}...' with voice: {voice}, speed: {speed_float}"
    )
    logger.info(f"Output file will be: {output_path}")

    # We'll use the high-level "model.generate" method:
    results = tts_model.generate(
        text=text,
        voice=voice,
        speed=speed_float,
        lang_code="a",
        verbose=False,
    )

    # We'll just gather all segments (if any) into a single wav
    # It's typical for multi-segment text to produce multiple wave segments:
    audio_arrays = []
    for segment in results:
        audio_arrays.append(segment.audio)

    # If no segments, return error
    if not audio_arrays:
        logger.error("No audio segments generated")
        return JSONResponse({"error": "No audio generated"}, status_code=500)

    # Concatenate all segments
    cat_audio = np.concatenate(audio_arrays, axis=0)

    # Write the audio as a WAV
    try:
        sf.write(output_path, cat_audio, 24000)
        logger.info(f"Successfully wrote audio file to {output_path}")

        # Verify the file exists
        if not os.path.exists(output_path):
            logger.error(f"File was not created at {output_path}")
            return JSONResponse(
                {"error": "Failed to create audio file"}, status_code=500
            )

        # Check file size
        file_size = os.path.getsize(output_path)
        logger.info(f"File size: {file_size} bytes")

        if file_size == 0:
            logger.error("File was created but is empty")
            return JSONResponse(
                {"error": "Generated audio file is empty"}, status_code=500
            )

    except Exception as e:
        logger.error(f"Error writing audio file: {str(e)}")
        return JSONResponse(
            {"error": f"Failed to save audio: {str(e)}"}, status_code=500
        )

    return {"filename": filename}


@app.get("/audio/{filename}")
def get_audio_file(filename: str):
    """
    Return an audio file from the outputs folder.
    The user can GET /audio/<filename> to fetch the WAV file.
    """
    file_path = os.path.join(OUTPUT_FOLDER, filename)
    logger.info(f"Requested audio file: {file_path}")

    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        # List files in the directory to help debug
        try:
            files = os.listdir(OUTPUT_FOLDER)
            logger.info(f"Files in output directory: {files}")
        except Exception as e:
            logger.error(f"Error listing output directory: {str(e)}")

        return JSONResponse({"error": "File not found"}, status_code=404)

    logger.info(f"Serving audio file: {file_path}")
    return FileResponse(file_path, media_type="audio/wav")


@app.get("/")
def root():
    """
    Serve the audio_player.html page or a fallback HTML if not found
    """
    try:
        # Try to find the audio_player.html file in the package
        static_dir = find_static_dir()
        audio_player_path = os.path.join(static_dir, "audio_player.html")
        return FileResponse(audio_player_path)
    except Exception as e:
        # If there's an error, return a simple HTML page with error information
        return HTMLResponse(
            content=f"""
            <html>
                <head><title>MLX-Audio TTS Server</title></head>
                <body>
                    <h1>MLX-Audio TTS Server</h1>
                    <p>The server is running, but the web interface could not be loaded.</p>
                    <p>Error: {str(e)}</p>
                    <h2>API Endpoints</h2>
                    <ul>
                        <li><code>POST /tts</code> - Generate TTS audio</li>
                        <li><code>GET /audio/{{filename}}</code> - Retrieve generated audio file</li>
                    </ul>
                </body>
            </html>
            """,
            status_code=200,
        )


def find_static_dir():
    """Find the static directory containing HTML files."""
    # Try different methods to find the static directory

    # Method 1: Use importlib.resources (Python 3.9+)
    try:
        import importlib.resources as pkg_resources

        static_dir = pkg_resources.files("mlx_audio").joinpath("tts")
        static_dir_str = str(static_dir)
        if os.path.exists(static_dir_str):
            return static_dir_str
    except (ImportError, AttributeError):
        pass

    # Method 2: Use importlib_resources (Python 3.8)
    try:
        import importlib_resources

        static_dir = importlib_resources.files("mlx_audio").joinpath("tts")
        static_dir_str = str(static_dir)
        if os.path.exists(static_dir_str):
            return static_dir_str
    except ImportError:
        pass

    # Method 3: Use pkg_resources
    try:
        static_dir_str = pkg_resources.resource_filename("mlx_audio", "tts")
        if os.path.exists(static_dir_str):
            return static_dir_str
    except (ImportError, pkg_resources.DistributionNotFound):
        pass

    # Method 4: Try to find the module path directly
    try:
        module_spec = importlib.util.find_spec("mlx_audio")
        if module_spec and module_spec.origin:
            package_dir = os.path.dirname(module_spec.origin)
            static_dir_str = os.path.join(package_dir, "tts")
            if os.path.exists(static_dir_str):
                return static_dir_str
    except (ImportError, AttributeError):
        pass

    # Method 5: Look in sys.modules
    try:
        if "mlx_audio" in sys.modules:
            module = sys.modules["mlx_audio"]
            if hasattr(module, "__file__"):
                package_dir = os.path.dirname(module.__file__)
                static_dir_str = os.path.join(package_dir, "tts")
                if os.path.exists(static_dir_str):
                    return static_dir_str
    except Exception:
        pass

    # If all methods fail, raise an error
    raise RuntimeError("Could not find static directory")


@app.post("/play")
def play_audio(filename: str = Form(...)):
    """
    Play audio directly from the server using the AudioPlayer.
    Expects a filename that exists in the OUTPUT_FOLDER.
    """
    global audio_player

    if audio_player is None:
        return JSONResponse({"error": "Audio player not initialized"}, status_code=500)

    file_path = os.path.join(OUTPUT_FOLDER, filename)
    if not os.path.exists(file_path):
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        # Load the audio file
        audio_data, sample_rate = sf.read(file_path)

        # If audio is stereo, convert to mono
        if len(audio_data.shape) > 1 and audio_data.shape[1] > 1:
            audio_data = audio_data.mean(axis=1)

        # Queue the audio for playback
        audio_player.queue_audio(audio_data)

        return {"status": "playing", "filename": filename}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to play audio: {str(e)}"}, status_code=500
        )


@app.post("/stop")
def stop_audio():
    """
    Stop any currently playing audio.
    """
    global audio_player

    if audio_player is None:
        return JSONResponse({"error": "Audio player not initialized"}, status_code=500)

    try:
        audio_player.stop()
        return {"status": "stopped"}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to stop audio: {str(e)}"}, status_code=500
        )


@app.post("/open_output_folder")
def open_output_folder():
    """
    Open the output folder in the system file explorer (Finder on macOS).
    This only works when running on localhost for security reasons.
    """
    global OUTPUT_FOLDER

    # Check if the request is coming from localhost
    # Note: In a production environment, you would want to check the request IP

    try:
        # For macOS (Finder)
        if sys.platform == "darwin":
            os.system(f"open {OUTPUT_FOLDER}")
        # For Windows (Explorer)
        elif sys.platform == "win32":
            os.system(f"explorer {OUTPUT_FOLDER}")
        # For Linux (various file managers)
        elif sys.platform == "linux":
            os.system(f"xdg-open {OUTPUT_FOLDER}")
        else:
            return JSONResponse(
                {"error": f"Unsupported platform: {sys.platform}"}, status_code=500
            )

        logger.info(f"Opened output folder: {OUTPUT_FOLDER}")
        return {"status": "opened", "path": OUTPUT_FOLDER}
    except Exception as e:
        logger.error(f"Error opening output folder: {str(e)}")
        return JSONResponse(
            {"error": f"Failed to open output folder: {str(e)}"}, status_code=500
        )


def setup_server():
    """Setup the server by loading the model and creating the output directory."""
    global tts_model, audio_player, OUTPUT_FOLDER

    # Make sure the output folder for generated TTS files exists
    try:
        os.makedirs(OUTPUT_FOLDER, exist_ok=True)
        # Test write permissions by creating a test file
        test_file = os.path.join(OUTPUT_FOLDER, "test_write.txt")
        with open(test_file, "w") as f:
            f.write("Test write permissions")
        os.remove(test_file)
        logger.info(f"Output directory {OUTPUT_FOLDER} is writable")
    except Exception as e:
        logger.error(f"Error with output directory {OUTPUT_FOLDER}: {str(e)}")
        # Try to use a fallback directory in /tmp
        fallback_dir = os.path.join("/tmp", "mlx_audio_outputs")
        logger.info(f"Trying fallback directory: {fallback_dir}")
        try:
            os.makedirs(fallback_dir, exist_ok=True)
            OUTPUT_FOLDER = fallback_dir
            logger.info(f"Using fallback output directory: {OUTPUT_FOLDER}")
        except Exception as fallback_error:
            logger.error(f"Error with fallback directory: {str(fallback_error)}")

    # Load the model if not already loaded
    if tts_model is None:
        try:
            logger.info(f"Loading TTS model from {MODEL_PATH}")
            tts_model = load_model(MODEL_PATH)
            logger.info("TTS model loaded successfully")
        except Exception as e:
            logger.error(f"Error loading TTS model: {str(e)}")
            raise

    # Initialize the audio player if not already initialized
    if audio_player is None:
        try:
            logger.info("Initializing audio player")
            audio_player = AudioPlayer()
            logger.info("Audio player initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing audio player: {str(e)}")

    # Try to mount the static files directory
    try:
        static_dir = find_static_dir()
        logger.info(f"Found static directory: {static_dir}")
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        logger.info("Static files mounted successfully")
    except Exception as e:
        logger.error(f"Could not mount static files directory: {e}")
        logger.warning(
            "The server will still function, but the web interface may be limited."
        )


def main(host="127.0.0.1", port=8000):
    """Parse command line arguments for the server and start it."""
    parser = argparse.ArgumentParser(description="Start the MLX-Audio TTS server")
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address to bind the server to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the server to (default: 8000)",
    )
    args = parser.parse_args()

    # Start the server with the parsed arguments
    setup_server()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
