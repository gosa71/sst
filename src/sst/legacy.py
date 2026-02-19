import functools
import json
import os
import inspect
import warnings
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class ShadowCapture:
    def __init__(self, storage_dir=".shadow_data"):
        warnings.warn(
            "ShadowCapture is deprecated. Use SSTCore from sst.core instead.",
            DeprecationWarning,
            stacklevel=2
        )
        self.storage_dir = storage_dir

    def capture(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            os.makedirs(self.storage_dir, exist_ok=True)
            
            # Capture inputs
            input_data = {
                "args": [repr(a) for a in args],
                "kwargs": {k: repr(v) for k, v in kwargs.items()}
            }
            
            start_time = datetime.now(timezone.utc)
            output_data = {"status": "unknown"}
            try:
                result = func(*args, **kwargs)
                duration = (datetime.now(timezone.utc) - start_time).total_seconds()
                
                # Capture output
                output_data = {
                    "result": repr(result),
                    "status": "success",
                    "duration": duration
                }
                return result
            except Exception as e:
                duration = (datetime.now(timezone.utc) - start_time).total_seconds()
                output_data = {
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "status": "failure",
                    "duration": duration
                }
                raise
            finally:
                # Save to file
                try:
                    capture_entry = {
                        "function": func.__name__,
                        "module": func.__module__,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "input": input_data,
                        "output": output_data,
                        "source": inspect.getsource(func)
                    }
                    
                    filename = f"{func.__module__}.{func.__name__}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.json"
                    with open(os.path.join(self.storage_dir, filename), "w") as f:
                        json.dump(capture_entry, f, indent=2)
                except Exception as write_err:
                    logger.warning("ShadowCapture: Failed to write capture data: %s", write_err)
        return wrapper

class _LazyShadow:
    """Lazy proxy that instantiates ShadowCapture only on first attribute access."""
    _instance = None

    def __getattr__(self, name):
        if self._instance is None:
            object.__setattr__(self, "_instance", ShadowCapture())
        return getattr(self._instance, name)


shadow = _LazyShadow()
