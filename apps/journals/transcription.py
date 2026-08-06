import io
import os
import threading
from contextlib import contextmanager

MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
MODEL_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
MODEL_COMPUTE = os.environ.get("WHISPER_COMPUTE_TYPE", "auto")

_model = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise RuntimeError(
                        "faster-whisper is not installed. "
                        "Install it with: pip install -r requirements.txt"
                    ) from exc
                _model = WhisperModel(
                    MODEL_NAME,
                    device=MODEL_DEVICE,
                    compute_type=MODEL_COMPUTE,
                )
    return _model


@contextmanager
def transcribing():
    with _inference_lock:
        yield


def transcribe_audio(data: bytes) -> str:
    model = _get_model()
    with transcribing():
        segments, _ = model.transcribe(
            io.BytesIO(data),
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=False,
            log_progress=False,
        )
        return "".join(segment.text for segment in segments).strip()
